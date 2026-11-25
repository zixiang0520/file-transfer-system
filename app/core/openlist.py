"""OpenList storage backend.

对接 OpenList（https://doc.oplist.org.cn/api/apidocs ，AList 的活跃分支）的 HTTP API，
把任意 OpenList 已挂载的网盘（移动云盘 / 阿里云盘 / 115 / OneDrive / S3 / 本地盘…）
当成文件流转系统的存储后端。

设计要点：
- 鉴权：优先用后台填写的 token；否则用 用户名 + 密码（可选 2FA）调
  ``POST /api/auth/login`` 换 token，token 缓存在内存并回写配置，过期自动重登；
- 写入：``PUT /api/fs/put``（``File-Path`` 头 + 原始字节流），文件对象来自
  FastAPI 的 SpooledTemporaryFile，大文件不会整包读进内存；
- 目录：``root/YYYY/MM/`` 分层，``POST /api/fs/mkdir`` 逐级创建；
- 读取：``POST /api/fs/get`` 取 ``sign``，拼 ``/d/<path>?sign=<sign>`` 直链
  （支持 Range、可 302 给浏览器），或由服务端流式中转；
- 删除：``POST /api/fs/remove``。
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from typing import Any, BinaryIO, Dict, Optional, Tuple
from urllib.parse import quote

import httpx

from app.config_store import load_config, proxy_dict, save_config

logger = logging.getLogger("fts.openlist")

_CHUNK = 1024 * 1024
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: Dict[str, str] = {}


class OpenListError(Exception):
    def __init__(self, message: str, code: int = 502):
        super().__init__(message)
        self.message = message
        self.code = code


def _safe_name(name: str) -> str:
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _SAFE_NAME_RE.sub("_", name).strip().strip(".")
    if not name:
        name = "file"
    if len(name) > 170:
        root, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 16:
            name = root[: 170 - len(ext) - 1] + "." + ext
        else:
            name = name[:170]
    return name


def _quote_path(path: str) -> str:
    """逐段 URL 编码，保留斜杠。"""
    parts = [p for p in (path or "").strip("/").split("/") if p]
    return "/".join(quote(p, safe="~()*!'") for p in parts)


def _norm_remote_path(path: str) -> str:
    """规范化成以 / 开头的原始（未编码）远端路径。"""
    rel = (path or "").strip().replace("\\", "/")
    if not rel.startswith("/"):
        rel = "/" + rel
    while "//" in rel:
        rel = rel.replace("//", "/")
    return rel.rstrip("/") or "/"


def _encode_path(path: str) -> str:
    """把原始远端路径编码成 URL 片段（保留前导斜杠）。"""
    out = _quote_path(path)
    return "/" + out if out else "/"


class OpenListClient:
    def __init__(self) -> None:
        cfg = load_config()
        o = (cfg.get("storage") or {}).get("openlist") or {}
        self.enabled = bool(o.get("enabled"))
        self.base = (o.get("url") or "").strip().rstrip("/")
        # 允许填 https://pan.example.com 或 https://pan.example.com/openlist
        if self.base.endswith("/api"):
            self.base = self.base[: -len("/api")]
        self.token = (o.get("token") or "").strip()
        if self.token.lower().startswith("bearer "):
            self.token = self.token[7:].strip()
        self.username = (o.get("username") or "").strip()
        self.password = (o.get("password") or "").strip()
        self.otp_code = (o.get("otp_code") or "").strip()
        self.root = (o.get("root") or "/").strip() or "/"
        self.verify_ssl = bool(o.get("verify_ssl", True))
        self.timeout = float(o.get("timeout") or 300)
        self.direct_link = bool(o.get("direct_link", True))
        self._client_ip = (o.get("client_ip") or "").strip()
        self._made_dirs: set[str] = set()

    # ---------- 基础 ----------

    def _client(self, timeout: Optional[float] = None) -> httpx.Client:
        kwargs: Dict[str, object] = {
            "timeout": httpx.Timeout(timeout or self.timeout, connect=20.0),
            "follow_redirects": True,
            "verify": self.verify_ssl,
        }
        try:
            px = proxy_dict() or {}
            one = (px.get("https://") or px.get("http://") or "").strip()
            if one:
                kwargs["proxy"] = one
        except Exception:
            pass
        return httpx.Client(**kwargs)

    def _url(self, path: str = "") -> str:
        if not self.base:
            raise OpenListError("未配置 OpenList 地址", 503)
        rel = (path or "").strip()
        if not rel:
            return self.base
        return f"{self.base}/{rel.lstrip('/')}"

    def require(self) -> None:
        if not self.enabled:
            raise OpenListError("OpenList 存储未启用，请在后台启用并填写地址", 503)
        if not self.base:
            raise OpenListError("未配置 OpenList 地址（如 http://192.168.1.10:5244）", 503)
        if not self.token and not (self.username and self.password):
            raise OpenListError("请填写 OpenList 用户名 + 密码，或直接填写 token", 503)

    # ---------- 鉴权 ----------

    def _cache_key(self) -> str:
        return f"{self.base}|{self.username}"

    def ensure_token(self, *, force: bool = False) -> str:
        """确保有可用 token：内存缓存 → 配置 → 账密登录。"""
        if self.token and not force:
            return self.token
        key = self._cache_key()
        with _TOKEN_LOCK:
            cached = _TOKEN_CACHE.get(key)
            if cached and not force:
                self.token = cached
                return cached
            if not (self.username and self.password):
                if self.token:
                    return self.token
                raise OpenListError("token 已失效，请重新填写 token 或配置用户名 + 密码", 401)
            payload: Dict[str, Any] = {
                "username": self.username,
                "password": self.password,
            }
            if self.otp_code:
                payload["otp_code"] = self.otp_code
            try:
                with self._client(timeout=60) as client:
                    r = client.post(self._url("/api/auth/login"), json=payload)
            except Exception as e:
                raise OpenListError(f"OpenList 登录请求失败: {e}", 502) from e
            body = _json_body(r)
            data = body.get("data") if isinstance(body, dict) else None
            token = str((data or {}).get("token") or "") if isinstance(data, dict) else ""
            if not token:
                msg = body.get("message") if isinstance(body, dict) else ""
                raise OpenListError(
                    f"OpenList 登录失败: {strip_html(str(msg or f'HTTP {r.status_code}'))}",
                    401,
                )
            _TOKEN_CACHE[key] = token
            self.token = token
            self._persist_token(token)
            logger.info("openlist login ok user=%s", self.username)
            return token

    def _persist_token(self, token: str) -> None:
        try:
            cfg = load_config()
            o = cfg.setdefault("storage", {}).setdefault("openlist", {})
            o["token"] = token
            o["token_saved_at"] = int(time.time())
            save_config(cfg)
        except Exception as e:  # 配置只读等情况不影响本次上传
            logger.warning("persist openlist token failed: %s", e)

    def _headers(self) -> Dict[str, str]:
        token = self.ensure_token()
        return {"Authorization": token}

    # ---------- 通用请求 ----------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        retried: bool = False,
        timeout: Optional[float] = None,
    ) -> Any:
        self.require()
        headers = self._headers()
        try:
            with self._client(timeout=timeout) as client:
                r = client.request(
                    method, self._url(path), json=json_body, headers=headers
                )
        except OpenListError:
            raise
        except Exception as e:
            raise OpenListError(f"OpenList 请求失败: {e}", 502) from e
        if r.status_code in (401, 403) and not retried:
            logger.info("openlist %s %s -> %s, retry login", method, path, r.status_code)
            self.ensure_token(force=True)
            return self._request(
                method, path, json_body=json_body, retried=True, timeout=timeout
            )
        body = _json_body(r)
        if r.status_code >= 400:
            msg = ""
            if isinstance(body, dict):
                msg = str(body.get("message") or "")
            raise OpenListError(
                f"OpenList HTTP {r.status_code}: {msg or r.text[:150]}",
                502,
            )
        if not isinstance(body, dict):
            raise OpenListError("OpenList 返回了非 JSON 响应（地址可能填错）", 502)
        code = body.get("code")
        if code is not None and int(code) != 200:
            raise OpenListError(
                strip_html(str(body.get("message") or f"code={code}")),
                401 if int(code) in (401, 403) else 502,
            )
        return body.get("data")

    # ---------- 目录 ----------

    def mkdir(self, remote_path: str) -> None:
        if remote_path in self._made_dirs:
            return
        try:
            self._request("POST", "/api/fs/mkdir", json_body={"path": remote_path}, timeout=60)
        except OpenListError as e:
            # 已存在 / 上层不存在等情况不致命，真正写入时会再报错
            logger.debug("openlist mkdir %s: %s", remote_path, e.message)
        self._made_dirs.add(remote_path)

    def _ensure_dirs(self, remote_dir: str) -> None:
        parts = [p for p in (remote_dir or "").strip("/").split("/") if p]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}"
            self.mkdir(cur)

    def _new_path(self, name: str) -> str:
        now = time.localtime()
        safe = _safe_name(name)
        rel_dir = "/".join(
            x
            for x in (
                (self.root or "/").strip("/"),
                time.strftime("%Y", now),
                time.strftime("%m", now),
            )
            if x
        )
        rel_dir = _norm_remote_path(rel_dir)
        self._ensure_dirs(rel_dir)
        fname = f"{uuid.uuid4().hex[:12]}_{safe}"
        return f"{rel_dir.rstrip('/')}/{fname}"

    # ---------- 上传 ----------

    def upload_stream(self, fileobj: BinaryIO, name: str, size: int) -> Dict[str, str]:
        self.require()
        remote_path = self._new_path(name)
        fileobj.seek(0)
        h = hashlib.sha256()
        read = 0
        while True:
            chunk = fileobj.read(_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
        fileobj.seek(0)
        if size and read != size:
            logger.warning(
                "openlist upload size mismatch: declared=%s actual=%s", size, read
            )
        headers = self._headers()
        headers.update(
            {
                "File-Path": _encode_path(remote_path),
                "Content-Type": "application/octet-stream",
                "Content-Length": str(read),
            }
        )
        try:
            with self._client() as client:
                req = client.build_request(
                    "PUT", self._url("/api/fs/put"), headers=headers, content=fileobj
                )
                r = client.send(req)
        except Exception as e:
            raise OpenListError(f"OpenList 上传失败: {e}", 502) from e
        if r.status_code in (401, 403):
            self.ensure_token(force=True)
            fileobj.seek(0)
            headers = self._headers()
            headers.update(
                {
                    "File-Path": _encode_path(remote_path),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(read),
                }
            )
            with self._client() as client:
                req = client.build_request(
                    "PUT", self._url("/api/fs/put"), headers=headers, content=fileobj
                )
                r = client.send(req)
        body = _json_body(r)
        if r.status_code >= 400 or not isinstance(body, dict) or int(body.get("code") or 0) != 200:
            msg = ""
            if isinstance(body, dict):
                msg = str(body.get("message") or "")
            raise OpenListError(
                f"OpenList 上传失败 HTTP {r.status_code}: {msg or r.text[:150]}", 502
            )
        return {
            "backend": "openlist",
            "storage_path": f"openlist://{remote_path}",
            "stored_name": remote_path.rsplit("/", 1)[-1],
            "size": str(read),
            "sha256": h.hexdigest(),
            "remote_id": remote_path,
        }

    def upload_bytes(self, content: bytes, name: str) -> Dict[str, str]:
        import io

        return self.upload_stream(io.BytesIO(content), name, len(content))

    # ---------- 下载 ----------

    def _direct_url(self, remote_path: str, *, timeout: Optional[float] = None) -> str:
        """取 sign 并拼 /d/<path>?sign= 直链。"""
        info = self._request(
            "POST",
            "/api/fs/get",
            json_body={"path": _norm_remote_path(remote_path)},
            timeout=timeout or 120,
        )
        if not isinstance(info, dict):
            raise OpenListError("OpenList 未返回文件信息", 502)
        sign = str(info.get("sign") or "")
        if not sign:
            raise OpenListError("OpenList 未返回下载签名（该存储可能不支持直链）", 502)
        return f"{self.base}/d/{_encode_path(remote_path).lstrip('/')}?sign={sign}"

    def download_bytes(self, remote_path: str) -> bytes:
        self.require()
        with self._client() as client:
            r = client.get(self._direct_url(remote_path))
        if r.status_code >= 400:
            raise OpenListError(f"OpenList 下载失败 HTTP {r.status_code}")
        return r.content

    def open_stream(
        self, remote_path: str, range_header: str = ""
    ) -> Tuple[httpx.Response, httpx.Client]:
        """打开流式 GET；调用方负责关闭 response 与 client。"""
        self.require()
        url = self._direct_url(remote_path)
        client = self._client()
        headers = {"Range": range_header} if range_header else {}
        req = client.build_request("GET", url, headers=headers)
        r = client.send(req, stream=True)
        if r.status_code >= 400:
            r.close()
            client.close()
            raise OpenListError(f"OpenList 下载失败 HTTP {r.status_code}")
        return r, client

    def get_download_url(self, remote_path: str) -> str:
        """返回可给浏览器 302 的直链；关闭直链时返回空串由服务端中转。"""
        if not self.direct_link:
            return ""
        try:
            return self._direct_url(remote_path, timeout=60)
        except OpenListError as e:
            logger.warning("openlist direct link failed: %s", e.message)
            return ""

    # ---------- 删除 / 测试 ----------

    def delete(self, remote_path: str) -> None:
        rel = _norm_remote_path(remote_path).strip("/")
        if not rel:
            return
        parent, _, name = rel.rpartition("/")
        try:
            self.require()
            self._request(
                "POST",
                "/api/fs/remove",
                json_body={"dir": "/" + parent.strip("/"), "names": [name]},
                timeout=60,
            )
        except Exception as e:
            logger.warning("openlist delete failed %s: %s", remote_path, e)

    def test_connection(self) -> Dict[str, object]:
        if not self.base:
            return {
                "ok": False,
                "error": "请填写 OpenList 地址，如 http://192.168.1.10:5244",
            }
        try:
            # 有账号密码时强制重新登录：验证的是凭据本身，而不是复用的缓存 token
            if self.username and self.password:
                _TOKEN_CACHE.pop(self._cache_key(), None)
                self.token = ""
                self.ensure_token(force=True)
            who = ""
            try:
                me = self._request("GET", "/api/me", timeout=30)
                if isinstance(me, dict):
                    who = str(me.get("username") or me.get("role") or "")
            except OpenListError:
                who = ""
            root = _norm_remote_path(self.root)
            self._ensure_dirs(root)
            listed = self._request(
                "POST",
                "/api/fs/list",
                json_body={
                    "path": root,
                    "page": 1,
                    "per_page": 1,
                    "refresh": False,
                },
                timeout=60,
            )
            provider = ""
            writable = None
            if isinstance(listed, dict):
                provider = str(listed.get("provider") or "")
                writable = listed.get("write")
            # 写权限探针
            probe = f"{root.rstrip('/')}/.fts_test_{uuid.uuid4().hex[:8]}.txt"
            headers = self._headers()
            headers.update(
                {
                    "File-Path": _encode_path(probe),
                    "Content-Type": "application/octet-stream",
                    "Content-Length": "2",
                }
            )
            with self._client(timeout=60) as client:
                r = client.put(self._url("/api/fs/put"), headers=headers, content=b"ok")
            body = _json_body(r)
            if not isinstance(body, dict) or int(body.get("code") or 0) != 200:
                msg = body.get("message") if isinstance(body, dict) else r.text[:150]
                return {
                    "ok": False,
                    "error": f"目录可读但写入失败（检查 OpenList 用户权限）: {msg}",
                }
            self.delete(probe)
            hint = f"OpenList 连接正常，根目录：{root}"
            if provider:
                hint += f"（驱动：{provider}）"
            if who:
                hint += f"，当前用户：{who}"
            if writable is False:
                hint += "；注意该目录标记为只读"
            return {
                "ok": True,
                "hint": hint,
                "root": root,
                "provider": provider,
                "backend": "openlist",
            }
        except OpenListError as e:
            return {"ok": False, "error": e.message}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ---------- 小工具 ----------

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub(" ", text or "").strip()[:200]


def _json_body(r: httpx.Response) -> Any:
    try:
        return r.json()
    except Exception:
        return {"code": r.status_code, "message": strip_html(r.text)}


def openlist_from_config() -> OpenListClient:
    return OpenListClient()
