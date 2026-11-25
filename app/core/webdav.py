"""WebDAV storage backend.

兼容 Alist / 坚果云 / Nextcloud / 群晖 / 飞牛自建 WebDAV 等标准 WebDAV 服务。

设计要点：
- 远端目录按 ``root/YYYY/MM/<uuid>_<safe_name>`` 组织，避免单目录文件过多；
- 上传走流式 PUT（文件对象来自 FastAPI 的 SpooledTemporaryFile，大文件自动落盘，
  不会整包读进内存）；
- 下载由服务端流式转发（WebDAV 需要 Basic 认证，不能把 URL 直接甩给浏览器），
  支持 Range 透传；
- 所有配置来自 ``data/config.json`` 的 ``storage.webdav``，密码仅存服务端。
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from typing import BinaryIO, Dict, Optional, Tuple

import httpx

from app.config_store import load_config, proxy_dict

logger = logging.getLogger("fts.webdav")

_CHUNK = 1024 * 1024
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class WebDAVError(Exception):
    def __init__(self, message: str, code: int = 502):
        super().__init__(message)
        self.message = message
        self.code = code


def _safe_name(name: str) -> str:
    """Keep unicode (中文) but strip path separators / control chars."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = _SAFE_NAME_RE.sub("_", name).strip().strip(".")
    if not name:
        name = "file"
    # 预留 uuid 前缀与扩展名，整体控制在 180 字符内
    if len(name) > 170:
        root, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 16:
            name = root[: 170 - len(ext) - 1] + "." + ext
        else:
            name = name[:170]
    return name


def _quote_path(path: str) -> str:
    """URL-encode each path segment, keep slashes."""
    from urllib.parse import quote

    parts = [p for p in (path or "").strip("/").split("/") if p]
    return "/".join(quote(p, safe="~()*!.'") for p in parts)


class WebDAVClient:
    def __init__(self) -> None:
        cfg = load_config()
        w = (cfg.get("storage") or {}).get("webdav") or {}
        self.enabled = bool(w.get("enabled"))
        self.base = (w.get("url") or "").strip().rstrip("/")
        self.username = (w.get("username") or "").strip()
        self.password = (w.get("password") or "").strip()
        self.root = (w.get("root") or "/").strip() or "/"
        self.verify_ssl = bool(w.get("verify_ssl", True))
        self.timeout = float(w.get("timeout") or 300)
        self._made_dirs: set[str] = set()

    # ---------- low level ----------

    def _auth(self):
        if self.username or self.password:
            return httpx.BasicAuth(self.username, self.password)
        return None

    def _client(self, timeout: Optional[float] = None) -> httpx.Client:
        kwargs: Dict[str, object] = {
            "timeout": httpx.Timeout(timeout or self.timeout, connect=20.0),
            "follow_redirects": True,
            "verify": self.verify_ssl,
            "auth": self._auth(),
        }
        try:
            px = proxy_dict() or {}
            one = (px.get("https://") or px.get("http://") or "").strip()
            if one:
                kwargs["proxy"] = one
        except Exception:
            pass
        return httpx.Client(**kwargs)

    def _url(self, rel_path: str) -> str:
        rel = (rel_path or "").strip("/")
        if not self.base:
            raise WebDAVError("未配置 WebDAV 地址", 503)
        return f"{self.base}/{_quote_path(rel)}" if rel else self.base + "/"

    def require(self) -> None:
        if not self.enabled:
            raise WebDAVError("WebDAV 存储未启用，请在后台启用并填写地址", 503)
        if not self.base:
            raise WebDAVError("未配置 WebDAV 地址", 503)

    # ---------- directory helpers ----------

    def _mkcol(self, rel_path: str) -> None:
        if rel_path in self._made_dirs:
            return
        with self._client(timeout=60) as client:
            r = client.request("MKCOL", self._url(rel_path))
        # 201 created / 405 already exists / 301 moved
        if r.status_code not in (200, 201, 204, 301, 405):
            raise WebDAVError(
                f"创建 WebDAV 目录失败 HTTP {r.status_code}: {r.text[:150]}"
            )
        self._made_dirs.add(rel_path)

    def _ensure_dirs(self, rel_dir: str) -> None:
        parts = [p for p in (rel_dir or "").strip("/").split("/") if p]
        cur = ""
        for p in parts:
            cur = f"{cur}/{p}" if cur else p
            self._mkcol(cur)

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
        self._ensure_dirs(rel_dir)
        return f"{rel_dir}/{uuid.uuid4().hex[:12]}_{safe}" if rel_dir else f"{uuid.uuid4().hex[:12]}_{safe}"

    # ---------- upload ----------

    def upload_stream(self, fileobj: BinaryIO, name: str, size: int) -> Dict[str, str]:
        """Stream a file-like object to WebDAV and return file metadata."""
        self.require()
        rel_path = self._new_path(name)
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
            logger.warning("webdav upload size mismatch: declared=%s actual=%s", size, read)
        with self._client() as client:
            r = client.put(
                self._url(rel_path),
                content=fileobj,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(read),
                },
            )
        if r.status_code not in (200, 201, 204):
            raise WebDAVError(f"WebDAV 上传失败 HTTP {r.status_code}: {r.text[:200]}")
        return {
            "backend": "webdav",
            "storage_path": f"webdav://{rel_path}",
            "stored_name": rel_path.rsplit("/", 1)[-1],
            "size": str(read),
            "sha256": h.hexdigest(),
            "remote_id": rel_path,
        }

    def upload_bytes(self, content: bytes, name: str) -> Dict[str, str]:
        import io

        return self.upload_stream(io.BytesIO(content), name, len(content))

    # ---------- download ----------

    def download_bytes(self, rel_path: str) -> bytes:
        self.require()
        with self._client() as client:
            r = client.get(self._url(rel_path))
        if r.status_code >= 400:
            raise WebDAVError(f"WebDAV 下载失败 HTTP {r.status_code}")
        return r.content

    def open_stream(
        self, rel_path: str, range_header: str = ""
    ) -> Tuple[httpx.Response, httpx.Client]:
        """Open a streaming GET; caller must close response and client."""
        self.require()
        client = self._client()
        headers = {"Range": range_header} if range_header else {}
        req = client.build_request("GET", self._url(rel_path), headers=headers)
        r = client.send(req, stream=True)
        if r.status_code >= 400:
            r.close()
            client.close()
            raise WebDAVError(f"WebDAV 下载失败 HTTP {r.status_code}")
        return r, client

    # ---------- delete / test ----------

    def delete(self, rel_path: str) -> None:
        if not rel_path:
            return
        try:
            self.require()
            with self._client(timeout=60) as client:
                r = client.delete(self._url(rel_path))
            if r.status_code not in (200, 202, 204, 404):
                logger.warning("webdav delete HTTP %s: %s", r.status_code, r.text[:120])
        except Exception as e:
            logger.warning("webdav delete failed %s: %s", rel_path, e)

    def test_connection(self) -> Dict[str, object]:
        if not self.base:
            return {"ok": False, "error": "请填写 WebDAV 地址（如 https://dav.example.com/dav）"}
        try:
            # 根目录不存在时先逐级创建（Alist/群晖/飞牛首次配置常见）
            self._ensure_dirs((self.root or "/").strip("/"))
            with self._client(timeout=30) as client:
                r = client.request(
                    "PROPFIND",
                    self._url((self.root or "/").strip("/")),
                    headers={"Depth": "0"},
                )
            if r.status_code not in (200, 207):
                return {
                    "ok": False,
                    "error": f"PROPFIND 失败 HTTP {r.status_code}: {r.text[:150]}",
                }
            # 写权限测试：PUT 一个探针文件再删除
            test_rel = "/".join(
                x for x in ((self.root or "/").strip("/"), f".fts_test_{uuid.uuid4().hex[:8]}.txt") if x
            )
            with self._client(timeout=30) as client:
                pr = client.put(self._url(test_rel), content=b"ok")
                if pr.status_code not in (200, 201, 204):
                    return {
                        "ok": False,
                        "error": f"目录可读但不可写 HTTP {pr.status_code}: {pr.text[:150]}",
                    }
                client.delete(self._url(test_rel))
            return {
                "ok": True,
                "hint": f"WebDAV 连接正常，根目录：{self.root or '/'}",
                "root": self.root or "/",
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}


def webdav_from_config() -> WebDAVClient:
    return WebDAVClient()
