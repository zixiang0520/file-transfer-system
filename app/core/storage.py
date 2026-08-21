"""Storage: 移动云盘(139 新个人云) only — no local disk.

根目录支持两种写法：
1) 真实 parentFileId（如 `/` 或接口返回的 fileId）
2) 显示路径（如 `/文件流转`、`文件流转/子目录`）— 自动从根目录按名称逐级解析，不存在则自动创建

鉴权对齐 OpenList 139：
- Authorization 可刷新（authTokenRefresh）
- 邮箱 Cookie + 用户名 + 密码可自动登录生成 Authorization
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import string
import time
import urllib.parse
from typing import Any, BinaryIO, Dict, Iterator, Optional, Tuple

import httpx

from app.config_store import load_config, proxy_dict, save_config
from app.core.yun139_auth import ensure_authorization

logger = logging.getLogger("fts.storage")

PERSONAL_HOST_DEFAULT = "https://personal-kd-njs.yun.139.com/hcy"
PART_SIZE = 100 * 1024 * 1024  # 100MB, same default as OpenList


class StorageError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def _encode_uri_component(s: str) -> str:
    return urllib.parse.quote(s, safe="~()*!.'")


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def cal_sign(body: str, ts: str, rand_str: str) -> str:
    body = _encode_uri_component(body)
    body = "".join(sorted(body))
    import base64

    body_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")
    res = _md5_hex(body_b64) + _md5_hex(f"{ts}:{rand_str}")
    return _md5_hex(res).upper()


def _rand16() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


class Yun139Client:
    """OpenList-compatible personal_new client with Authorization refresh + password fallback."""

    def __init__(self) -> None:
        cfg = load_config()
        y = (cfg.get("storage") or {}).get("yun139") or {}
        self.auth = (y.get("authorization") or "").strip()
        if self.auth.lower().startswith("basic "):
            self.auth = self.auth[6:].strip()
        self.root = (y.get("root_folder_id") or "/").strip() or "/"
        self.username = (y.get("username") or "").strip()
        self.password = (y.get("password") or "").strip()
        self.mail_cookies = (y.get("mail_cookies") or "").strip()
        self.has_password_login = bool(self.username and self.password and self.mail_cookies)
        self.enabled = bool(y.get("enabled") and (self.auth or self.has_password_login))
        self.host = (y.get("personal_cloud_host") or PERSONAL_HOST_DEFAULT).rstrip("/")
        self.account = (y.get("account_hint") or y.get("username") or "").strip()
        self._resolved_parent: Optional[str] = None
        self._cached_resolved = (y.get("resolved_folder_id") or "").strip()
        self._auth_ensured = False

    def require(self) -> None:
        if not self.enabled:
            raise StorageError(
                "请先在后台启用移动云盘，并配置 Authorization 或（邮箱 Cookie + 用户名 + 密码）",
                503,
            )
        self.ensure_auth()

    def ensure_auth(self, *, force_login: bool = False) -> None:
        """Refresh or password-login Authorization; persist when changed."""
        if self._auth_ensured and self.auth and not force_login:
            return
        try:
            out = ensure_authorization(
                self.auth,
                self.username,
                self.password,
                self.mail_cookies,
                force_login=force_login,
            )
        except Exception as e:
            raise StorageError(f"云盘鉴权失败: {e}", 502) from e
        new_auth = (out.get("authorization") or "").strip()
        if new_auth.lower().startswith("basic "):
            new_auth = new_auth[6:].strip()
        changed = new_auth and new_auth != self.auth
        self.auth = new_auth or self.auth
        if out.get("account"):
            self.account = out["account"]
        if out.get("mail_cookies"):
            self.mail_cookies = out["mail_cookies"]
        if changed or out.get("refreshed") == "1" or out.get("user_domain_id"):
            self._persist_auth(
                authorization=self.auth,
                mail_cookies=out.get("mail_cookies") or None,
                account=out.get("account") or None,
                user_domain_id=out.get("user_domain_id") or None,
            )
        self._auth_ensured = True
        logger.info(
            "yun139 ensure_auth source=%s refreshed=%s",
            out.get("source"),
            out.get("refreshed"),
        )

    def _persist_auth(
        self,
        *,
        authorization: str,
        mail_cookies: Optional[str] = None,
        account: Optional[str] = None,
        user_domain_id: Optional[str] = None,
    ) -> None:
        try:
            cfg = load_config()
            y = cfg.setdefault("storage", {}).setdefault("yun139", {})
            if authorization:
                y["authorization"] = authorization
            if mail_cookies:
                y["mail_cookies"] = mail_cookies
            if account:
                y["account_hint"] = account
            if user_domain_id:
                y["user_domain_id"] = user_domain_id
            save_config(cfg)
        except Exception as e:
            logger.warning("persist authorization failed: %s", e)

    def _headers(self, body_str: str) -> Dict[str, str]:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        rand_str = _rand16()
        sign = cal_sign(body_str, ts, rand_str)
        return {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Basic {self.auth}",
            "Content-Type": "application/json;charset=UTF-8",
            "Caller": "web",
            "Cms-Device": "default",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Route": "001",
            "Mcloud-Sign": f"{ts},{rand_str},{sign}",
            "Mcloud-Version": "7.14.0",
            "x-DeviceInfo": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||",
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
            "X-Yun-Api-Version": "v1",
            "X-Yun-App-Channel": "10000034",
            "X-Yun-Channel-Source": "10000034",
            "X-Yun-Client-Info": "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||",
            "X-Yun-Module-Type": "100",
            "X-Yun-Svc-Type": "1",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
            "User-Agent": "Mozilla/5.0 FileTransferSystem/1.0",
        }

    def _client(self) -> httpx.Client:
        # httpx>=0.28 uses proxy= (singular), not proxies=
        kwargs: Dict[str, object] = {"timeout": 120.0, "follow_redirects": True}
        try:
            px = proxy_dict() or {}
            one = (px.get("https://") or px.get("http://") or "").strip()
            if one:
                kwargs["proxy"] = one
        except Exception:
            pass
        return httpx.Client(**kwargs)

    def personal_post(self, pathname: str, data: dict, *, _retried: bool = False) -> dict:
        self.require()
        body_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        url = f"{self.host}{pathname}"
        headers = self._headers(body_str)
        with self._client() as client:
            r = client.post(url, content=body_str.encode("utf-8"), headers=headers)
        try:
            j = r.json()
        except Exception:
            raise StorageError(f"云盘响应无效 HTTP {r.status_code}: {r.text[:200]}", 502)
        msg = str(j.get("message") or "")
        code = str(j.get("code") or "")
        auth_fail = (
            "认证失败" in msg
            or "05050006" in msg
            or code in ("05050006", "401", "403")
            or (j.get("success") is False and "auth" in msg.lower())
        )
        if auth_fail and not _retried and self.has_password_login:
            logger.warning("yun139 auth fail on %s (%s), re-login…", pathname, msg or code)
            self._auth_ensured = False
            self.ensure_auth(force_login=True)
            return self.personal_post(pathname, data, _retried=True)
        if r.status_code >= 400:
            logger.warning("yun139 %s HTTP %s: %s", pathname, r.status_code, r.text[:400])
            raise StorageError(j.get("message") or f"HTTP {r.status_code}", r.status_code)
        if j.get("success") is False:
            logger.warning("yun139 %s success=false: %s", pathname, r.text[:400])
            raise StorageError(j.get("message") or j.get("code") or "云盘请求失败", 502)
        if code and code not in ("0000", "0", "success"):
            if not j.get("success", True) and not j.get("data"):
                logger.warning("yun139 %s biz err %s: %s", pathname, code, r.text[:400])
                raise StorageError(j.get("message") or code, 502)
        return j

    # ---------- folder path resolve / auto-create ----------

    def _list_children(self, parent_file_id: str) -> list:
        items: list = []
        cursor = ""
        while True:
            j = self.personal_post(
                "/file/list",
                {
                    "imageThumbnailStyleList": ["Small", "Large"],
                    "orderBy": "updated_at",
                    "orderDirection": "DESC",
                    "pageInfo": {"pageCursor": cursor, "pageSize": 100},
                    "parentFileId": parent_file_id,
                },
            )
            data = j.get("data") or {}
            batch = data.get("items") or []
            items.extend(batch)
            cursor = (data.get("nextPageCursor") or data.get("pageCursor") or "") or ""
            if not cursor or not batch:
                break
        return items

    def _try_list(self, parent_file_id: str) -> bool:
        try:
            self._list_children(parent_file_id)
            return True
        except Exception:
            return False

    @staticmethod
    def _looks_like_display_path(s: str) -> bool:
        """用户填的是显示路径（含中文名）而不是 raw fileId。"""
        if not s or s == "/":
            return False
        if any(ord(ch) > 127 for ch in s):
            return True
        parts = [x for x in s.strip("/").split("/") if x]
        return len(parts) >= 2

    def create_folder(self, parent_file_id: str, name: str) -> str:
        self.personal_post(
            "/file/create",
            {
                "parentFileId": parent_file_id,
                "name": name,
                "description": "",
                "type": "folder",
                "fileRenameMode": "force_rename",
            },
        )
        for it in self._list_children(parent_file_id):
            if it.get("name") == name and it.get("type") == "folder":
                fid = str(it.get("fileId") or "")
                if fid:
                    return fid
        raise StorageError(f"创建文件夹失败: {name}", 502)

    def resolve_parent_id(self, path_or_id: Optional[str] = None, *, auto_create: bool = True) -> str:
        """把 `/文件流转` 这类显示路径解析成真实 parentFileId；缺失目录可自动创建。"""
        if self._resolved_parent:
            return self._resolved_parent

        raw = (path_or_id if path_or_id is not None else self.root) or "/"
        raw = str(raw).strip() or "/"
        if raw == "/":
            self._resolved_parent = "/"
            return "/"

        # 上次解析结果仍有效
        if self._cached_resolved and self._try_list(self._cached_resolved):
            self._resolved_parent = self._cached_resolved
            return self._resolved_parent

        # 看起来像 raw id 且 list 成功 → 直接用
        if not self._looks_like_display_path(raw) and self._try_list(raw):
            self._resolved_parent = raw
            return raw

        # 按路径名逐级走：/文件流转/子目录
        parts = [x for x in raw.strip("/").split("/") if x]
        if not parts:
            self._resolved_parent = "/"
            return "/"

        cur = "/"
        for name in parts:
            children = self._list_children(cur)
            found = None
            for it in children:
                if it.get("type") == "folder" and it.get("name") == name:
                    found = str(it.get("fileId") or "")
                    break
            if found:
                cur = found
                continue
            if not auto_create:
                raise StorageError(f"云盘中找不到文件夹「{name}」（路径 {raw}）", 404)
            logger.info("yun139 auto-create folder %r under %s", name, cur)
            cur = self.create_folder(cur, name)

        self._resolved_parent = cur
        return cur

    def _persist_resolved(self, resolved: str) -> None:
        try:
            cfg = load_config()
            y = cfg.setdefault("storage", {}).setdefault("yun139", {})
            y["resolved_folder_id"] = resolved
            save_config(cfg)
            self._cached_resolved = resolved
        except Exception as e:
            logger.warning("persist resolved_folder_id failed: %s", e)

    def test_connection(self) -> Dict[str, object]:
        if not self.auth and not self.has_password_login:
            return {
                "ok": False,
                "error": "请配置 Authorization，或同时填写邮箱 Cookie + 用户名 + 密码（OpenList 长期方案）",
            }
        try:
            old = self.enabled
            self.enabled = True
            self.ensure_auth()
            resolved = self.resolve_parent_id(self.root, auto_create=True)
            j = self.personal_post(
                "/file/list",
                {
                    "imageThumbnailStyleList": ["Small", "Large"],
                    "orderBy": "updated_at",
                    "orderDirection": "DESC",
                    "pageInfo": {"pageCursor": "", "pageSize": 10},
                    "parentFileId": resolved,
                },
            )
            self.enabled = old
            ok = bool(
                j.get("success") is True
                or j.get("data") is not None
                or str(j.get("code")) in ("0000", "0")
            )
            self._persist_resolved(resolved)
            hint = f"连接成功；上传目录已解析为 {resolved}"
            if self.root not in ("/", resolved) and self.root != resolved:
                hint += f"（由「{self.root}」自动解析/创建）"
            if self.has_password_login:
                hint += "；已启用密码登录回退（Authorization 可自动续期）"
            return {
                "ok": ok,
                "status": 200,
                "resolved_folder_id": resolved,
                "input_root": self.root,
                "hint": hint if ok else "已返回数据但业务码异常，请核对 Authorization / 邮箱 Cookie",
                "raw": json.dumps(j, ensure_ascii=False)[:300],
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def create_upload_task(self, name: str, size: int, sha256: str) -> Dict[str, Any]:
        """Create a 139 upload task and return presigned part URLs so the browser
        can PUT parts directly to the cloud (no server relay)."""
        self.require()
        parent = self.resolve_parent_id(self.root, auto_create=True)
        self._persist_resolved(parent)
        part_size = PART_SIZE
        n_parts = max(1, (size + part_size - 1) // part_size)
        part_infos = []
        for i in range(n_parts):
            start = i * part_size
            byte_size = min(size - start, part_size)
            part_infos.append(
                {
                    "partNumber": i + 1,
                    "partSize": byte_size,
                    "parallelHashCtx": {"partOffset": start},
                }
            )
        first = part_infos[:100]
        create_body = {
            "contentHash": sha256,
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": first,
            "size": size,
            "parentFileId": parent,
            "name": name,
            "type": "file",
            "fileRenameMode": "auto_rename",
        }
        resp = self.personal_post("/file/create", create_body)
        data = resp.get("data") or {}
        file_id = data.get("fileId") or ""
        upload_id = data.get("uploadId") or ""
        parts = []
        for up in data.get("partInfos") or []:
            pn = int(up.get("partNumber") or (len(parts) + 1))
            byte_size = min(size - (pn - 1) * part_size, part_size)
            parts.append(
                {
                    "partNumber": pn,
                    "partSize": byte_size,
                    "uploadUrl": up.get("uploadUrl") or up.get("cdnUploadUrl") or "",
                }
            )
        return {
            "file_id": file_id,
            "upload_id": upload_id,
            "exist": bool(data.get("exist") and file_id),
            "rapid": bool(data.get("rapidUpload")),
            "file_name": data.get("fileName") or name,
            "part_size": part_size,
            "size": size,
            "sha256": sha256,
            "parts": parts,
        }

    def complete_upload(self, file_id: str, upload_id: str, sha256: str) -> None:
        """Finalize a 139 upload after all parts have been PUT by the client."""
        self.require()
        self.personal_post(
            "/file/complete",
            {
                "contentHash": sha256,
                "contentHashAlgorithm": "SHA256",
                "fileId": file_id,
                "uploadId": upload_id,
            },
        )

    def upload_bytes(self, content: bytes, name: str) -> Dict[str, str]:
        """Upload file content to 139 personal cloud. Returns meta with remote_id."""
        self.require()
        parent = self.resolve_parent_id(self.root, auto_create=True)
        self._persist_resolved(parent)
        size = len(content)
        full_hash = hashlib.sha256(content).hexdigest()
        part_size = PART_SIZE
        n_parts = max(1, (size + part_size - 1) // part_size)
        part_infos = []
        for i in range(n_parts):
            start = i * part_size
            byte_size = min(size - start, part_size)
            part_infos.append(
                {
                    "partNumber": i + 1,
                    "partSize": byte_size,
                    "parallelHashCtx": {"partOffset": start},
                }
            )
        first = part_infos[:100]
        create_body = {
            "contentHash": full_hash,
            "contentHashAlgorithm": "SHA256",
            "contentType": "application/octet-stream",
            "parallelUpload": False,
            "partInfos": first,
            "size": size,
            "parentFileId": parent,
            "name": name,
            "type": "file",
            "fileRenameMode": "auto_rename",
        }
        resp = self.personal_post("/file/create", create_body)
        data = resp.get("data") or {}
        file_id = data.get("fileId") or ""
        upload_id = data.get("uploadId") or ""
        if data.get("exist") and file_id:
            return {
                "backend": "yun139",
                "storage_path": f"yun139://{file_id}",
                "stored_name": data.get("fileName") or name,
                "size": str(size),
                "sha256": full_hash,
                "remote_id": file_id,
            }
        upload_parts = data.get("partInfos") or []
        if upload_parts:
            self._put_parts(content, part_infos, upload_parts)
            for i in range(100, len(part_infos), 100):
                batch = part_infos[i : i + 100]
                more = self.personal_post(
                    "/file/getUploadUrl",
                    {
                        "fileId": file_id,
                        "uploadId": upload_id,
                        "partInfos": batch,
                        "commonAccountInfo": {
                            "account": self.account or "0",
                            "accountType": 1,
                        },
                    },
                )
                more_parts = (more.get("data") or {}).get("partInfos") or []
                self._put_parts(content, part_infos, more_parts)
            self.personal_post(
                "/file/complete",
                {
                    "contentHash": full_hash,
                    "contentHashAlgorithm": "SHA256",
                    "fileId": file_id,
                    "uploadId": upload_id,
                },
            )
        elif not file_id:
            raise StorageError("云盘创建上传任务失败：无 fileId", 502)

        if not file_id:
            file_id = data.get("fileId") or ""
        return {
            "backend": "yun139",
            "storage_path": f"yun139://{file_id}",
            "stored_name": data.get("fileName") or name,
            "size": str(size),
            "sha256": full_hash,
            "remote_id": file_id,
        }

    def _put_parts(self, content: bytes, part_infos: list, upload_parts: list) -> None:
        upload_parts = sorted(upload_parts, key=lambda x: int(x.get("partNumber") or 0))
        with self._client() as client:
            for up in upload_parts:
                pn = int(up.get("partNumber") or 0)
                idx = pn - 1
                if idx < 0 or idx >= len(part_infos):
                    raise StorageError(f"分片编号异常: {pn}")
                info = part_infos[idx]
                start = int(info["parallelHashCtx"]["partOffset"])
                length = int(info["partSize"])
                chunk = content[start : start + length]
                url = up.get("uploadUrl") or ""
                if not url:
                    raise StorageError("缺少分片上传地址")
                r = client.put(
                    url,
                    content=chunk,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(length),
                        "Origin": "https://yun.139.com",
                        "Referer": "https://yun.139.com/",
                    },
                )
                if r.status_code not in (200, 201):
                    raise StorageError(f"分片上传失败 HTTP {r.status_code}: {r.text[:200]}", 502)

    def get_download_url(self, file_id: str) -> str:
        self.require()
        j = self.personal_post("/file/getDownloadUrl", {"fileId": file_id})
        data = j.get("data") or {}
        if data.get("cdnSwitch") and data.get("cdnUrl"):
            return str(data["cdnUrl"])
        url = data.get("url") or data.get("cdnUrl") or ""
        if not url:
            raise StorageError("无法获取云盘下载链接", 502)
        return str(url)

    def download_bytes(self, file_id: str) -> bytes:
        url = self.get_download_url(file_id)
        with self._client() as client:
            r = client.get(url)
            if r.status_code >= 400:
                raise StorageError(f"云盘下载失败 HTTP {r.status_code}", 502)
            return r.content

    def stream_download(self, file_id: str) -> Tuple[str, Iterator[bytes], Optional[str]]:
        url = self.get_download_url(file_id)
        client = self._client()
        r = client.stream("GET", url)
        r.__enter__()
        if r.status_code >= 400:
            r.__exit__(None, None, None)
            client.close()
            raise StorageError(f"云盘下载失败 HTTP {r.status_code}", 502)
        cl = r.headers.get("content-length")

        def gen():
            try:
                for chunk in r.iter_bytes(1024 * 256):
                    yield chunk
            finally:
                r.__exit__(None, None, None)
                client.close()

        return url, gen(), cl

    def delete_file(self, file_id: str) -> None:
        if not file_id:
            return
        try:
            self.require()
            self.personal_post("/recyclebin/batchTrash", {"fileIds": [file_id]})
        except Exception as e:
            logger.warning("yun139 delete failed %s: %s", file_id, e)


def _remote_id_from_meta(storage_path: str, remote_id: str = "") -> str:
    if remote_id:
        return remote_id
    if storage_path.startswith("yun139://"):
        return storage_path[len("yun139://") :]
    return storage_path


def save_file(fileobj: BinaryIO, original_name: str) -> Dict[str, str]:
    yun = Yun139Client()
    yun.require()
    data = fileobj.read()
    if isinstance(data, memoryview):
        data = data.tobytes()
    return yun.upload_bytes(data, original_name)


def create_upload_task(name: str, size: int, sha256: str) -> Dict[str, Any]:
    """Module-level wrapper: create 139 upload task with presigned part URLs."""
    yun = Yun139Client()
    return yun.create_upload_task(name, size, sha256)


def complete_upload(file_id: str, upload_id: str, sha256: str) -> None:
    """Module-level wrapper: finalize a direct 139 upload."""
    yun = Yun139Client()
    yun.complete_upload(file_id, upload_id, sha256)


def open_file_bytes(storage_path: str, remote_id: str = "") -> bytes:
    yun = Yun139Client()
    fid = _remote_id_from_meta(storage_path, remote_id)
    return yun.download_bytes(fid)


def get_download_url(storage_path: str, remote_id: str = "") -> str:
    yun = Yun139Client()
    fid = _remote_id_from_meta(storage_path, remote_id)
    return yun.get_download_url(fid)


def delete_file(backend: str, storage_path: str, remote_id: str = "") -> None:
    fid = _remote_id_from_meta(storage_path, remote_id)
    if not fid:
        return
    if storage_path and not storage_path.startswith("yun139://") and "/" in storage_path and not remote_id:
        try:
            from pathlib import Path

            p = Path(storage_path)
            if p.exists() and p.is_file():
                p.unlink()
        except Exception:
            pass
        return
    Yun139Client().delete_file(fid)
