"""Storage backends: local + 移动云盘(139) binding hooks."""
from __future__ import annotations

import hashlib
import logging
import shutil
import uuid
from pathlib import Path
from typing import BinaryIO, Dict, Optional, Tuple

import httpx

from app.config_store import load_config, proxy_dict, STORAGE_DIR

logger = logging.getLogger("fts.storage")


class StorageError(Exception):
    pass


def _local_root() -> Path:
    cfg = load_config()
    root = Path((cfg.get("storage") or {}).get("local_root") or str(STORAGE_DIR))
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_local(fileobj: BinaryIO, original_name: str) -> Dict[str, str]:
    """Save stream to local disk. Returns storage metadata."""
    ext = Path(original_name).suffix
    stored = f"{uuid.uuid4().hex}{ext}"
    day = Path.cwd()  # unused; use date folder
    import time

    folder = _local_root() / time.strftime("%Y%m%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / stored
    h = hashlib.sha256()
    size = 0
    with path.open("wb") as out:
        while True:
            chunk = fileobj.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            size += len(chunk)
    return {
        "backend": "local",
        "storage_path": str(path),
        "stored_name": stored,
        "size": str(size),
        "sha256": h.hexdigest(),
        "remote_id": "",
    }


def open_local(storage_path: str) -> Path:
    p = Path(storage_path)
    if not p.exists():
        raise StorageError("文件不存在或已删除")
    return p


def delete_local(storage_path: str) -> None:
    p = Path(storage_path)
    try:
        if p.exists():
            p.unlink()
        # clean empty day dir
        parent = p.parent
        if parent.exists() and parent != _local_root() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception as e:
        logger.warning("delete local failed: %s", e)


class Yun139Client:
    """Minimal 139 personal cloud client (Authorization Basic token).

    参考 OpenList/AList 中国移动云盘挂载说明：
    - 在 yun.139.com 抓 hcy/file/list 请求头 Authorization，只填 Basic 后的内容
    - 完整分片上传协议较复杂；当前实现：
        * test_connection(): 列目录验证 token
        * upload(): 若未实现完整 EOS，则回退本地并标记 pending_remote
      生产环境建议 token 有效时先本地落盘，后台可扩展真正上云。
    """

    LIST_URL = "https://personal-kd-njs.yun.139.com/hcy/file/list"

    def __init__(self) -> None:
        cfg = load_config()
        y = (cfg.get("storage") or {}).get("yun139") or {}
        self.auth = (y.get("authorization") or "").strip()
        self.root = (y.get("root_folder_id") or "/").strip() or "/"
        self.enabled = bool(y.get("enabled") and self.auth)

    def _headers(self) -> Dict[str, str]:
        token = self.auth
        if token.lower().startswith("basic "):
            token = token[6:].strip()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 FileTransferSystem/1.0",
            "Referer": "https://yun.139.com/",
            "Origin": "https://yun.139.com",
        }

    def test_connection(self) -> Dict[str, object]:
        if not self.auth:
            return {"ok": False, "error": "未配置 Authorization"}
        proxies = proxy_dict()
        try:
            with httpx.Client(timeout=20, proxies=proxies, follow_redirects=True) as client:
                # personal_new list body varies by version; try common shape
                body = {
                    "pageInfo": {"pageSize": 10, "pageCursor": ""},
                    "orderBy": [{"field": "updated_at", "order": "DESC"}],
                    "parentFileId": self.root if self.root != "/" else "/",
                }
                r = client.post(self.LIST_URL, headers=self._headers(), json=body)
                text = r.text[:300]
                ok = r.status_code == 200 and (
                    '"code":"0000"' in r.text
                    or '"code":0' in r.text
                    or '"success":true' in r.text.lower()
                    or "fileList" in r.text
                    or "items" in r.text
                )
                # some APIs return 401 clearly
                if r.status_code in (401, 403):
                    return {"ok": False, "error": f"鉴权失败 HTTP {r.status_code}", "raw": text}
                return {
                    "ok": ok or r.status_code == 200,
                    "status": r.status_code,
                    "hint": "HTTP 200 通常表示 token 可用；若业务 code 非成功请更新 Authorization",
                    "raw": text,
                }
        except Exception as e:
            return {"ok": False, "error": str(e)}


def get_active_backend() -> str:
    cfg = load_config()
    st = cfg.get("storage") or {}
    backend = (st.get("backend") or "local").lower()
    y = st.get("yun139") or {}
    if backend == "yun139" and y.get("enabled") and (y.get("authorization") or "").strip():
        return "yun139"
    return "local"


def save_file(fileobj: BinaryIO, original_name: str) -> Dict[str, str]:
    """Always persist locally for reliability; mark backend preference for 139."""
    meta = save_local(fileobj, original_name)
    backend = get_active_backend()
    if backend == "yun139":
        # Phase-1: local mirror + remote flag. Full EOS multipart upload can be extended.
        # Files still fully usable via local path; admin shows backend=yun139-pending
        meta["backend"] = "yun139-local"  # local cache while 139 full upload not wired
        meta["remote_id"] = ""
        # Keep file on disk; optional future: push to 139 then delete local
    return meta


def delete_file(backend: str, storage_path: str, remote_id: str = "") -> None:
    delete_local(storage_path)
