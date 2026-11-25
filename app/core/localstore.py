"""Local disk storage backend.

适用场景：NAS 本机盘 / 挂载的共享目录 / 内网无云盘环境。文件直接落盘到
``storage.local.root``，按 ``年/月/<uuid>_<safe_name>`` 组织；下载由服务端
流式转发（不暴露真实路径），支持 Range。

配置来自 ``data/config.json`` 的 ``storage.local``。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, Tuple

import httpx

from app.config_store import STORAGE_DIR, load_config

logger = logging.getLogger("fts.local")

_CHUNK = 1024 * 1024
_SAFE_NAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class LocalStorageError(Exception):
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


class LocalStream:
    """Small httpx.Response-like wrapper so the download route can stay generic."""

    def __init__(self, path: Path, range_header: str = "") -> None:
        self.path = path
        self.size = path.stat().st_size
        self.start = 0
        self.end = max(0, self.size - 1)
        self.status_code = 200
        headers = {
            "content-type": "application/octet-stream",
            "accept-ranges": "bytes",
            "content-length": str(self.size),
        }
        if range_header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    self.start = int(m.group(1))
                    if m.group(2):
                        self.end = min(int(m.group(2)), self.size - 1)
                else:  # suffix range: bytes=-N
                    self.start = max(0, self.size - int(m.group(2)))
                if self.start <= self.end and self.start < self.size:
                    self.status_code = 206
                    headers["content-range"] = f"bytes {self.start}-{self.end}/{self.size}"
                    headers["content-length"] = str(self.end - self.start + 1)
                else:
                    self.status_code = 416
                    headers["content-range"] = f"bytes */{self.size}"
                    headers["content-length"] = "0"
        self.headers = httpx.Headers(headers)
        self._f = None

    def iter_bytes(self, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
        if self.status_code == 416:
            return
        with open(self.path, "rb") as f:
            f.seek(self.start)
            left = self.end - self.start + 1
            while left > 0:
                data = f.read(min(chunk_size, left))
                if not data:
                    break
                left -= len(data)
                yield data

    def close(self) -> None:
        if self._f is not None:
            try:
                self._f.close()
            except Exception:
                pass
            self._f = None


class _NoopCloser:
    def close(self) -> None:
        return None


class LocalStorageClient:
    def __init__(self) -> None:
        cfg = load_config()
        loc = (cfg.get("storage") or {}).get("local") or {}
        self.enabled = bool(loc.get("enabled"))
        raw_root = (loc.get("root") or "").strip()
        self.root = Path(raw_root).expanduser() if raw_root else STORAGE_DIR

    def require(self) -> None:
        if not self.enabled:
            raise LocalStorageError("本地存储未启用，请在后台启用", 503)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise LocalStorageError(f"本地存储目录不可用：{e}", 503) from e
        if not os.access(self.root, os.W_OK):
            raise LocalStorageError(f"本地存储目录不可写：{self.root}", 503)

    def _abs(self, rel_path: str) -> Path:
        rel = (rel_path or "").lstrip("/").replace("\\", "/")
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root.resolve())
        except ValueError as e:
            raise LocalStorageError("非法的存储路径", 400) from e
        return p

    def _new_rel_path(self, name: str) -> str:
        now = time.localtime()
        rel_dir = "/".join(
            x for x in (time.strftime("%Y", now), time.strftime("%m", now)) if x
        )
        return f"{rel_dir}/{uuid.uuid4().hex[:12]}_{_safe_name(name)}"

    def upload_stream(
        self, fileobj: BinaryIO, name: str, size: int = 0
    ) -> Dict[str, str]:
        self.require()
        rel_path = self._new_rel_path(name)
        dest = self._abs(rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fileobj.seek(0)
        h = hashlib.sha256()
        read = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with open(tmp, "wb") as out:
                while True:
                    chunk = fileobj.read(_CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
                    read += len(chunk)
            if size and read != size:
                logger.warning(
                    "local upload size mismatch: declared=%s actual=%s", size, read
                )
            tmp.replace(dest)
        except Exception as e:
            try:
                tmp.unlink()
            except Exception:
                pass
            raise LocalStorageError(f"本地写入失败：{e}", 502) from e
        return {
            "backend": "local",
            "storage_path": f"local://{rel_path}",
            "stored_name": rel_path.rsplit("/", 1)[-1],
            "size": str(read),
            "sha256": h.hexdigest(),
            "remote_id": rel_path,
        }

    def download_bytes(self, rel_path: str) -> bytes:
        self.require()
        p = self._abs(rel_path)
        if not p.is_file():
            raise LocalStorageError("本地文件不存在", 404)
        return p.read_bytes()

    def open_stream(
        self, rel_path: str, range_header: str = ""
    ) -> Tuple[LocalStream, _NoopCloser]:
        self.require()
        p = self._abs(rel_path)
        if not p.is_file():
            raise LocalStorageError("本地文件不存在", 404)
        return LocalStream(p, range_header), _NoopCloser()

    def delete(self, rel_path: str) -> None:
        if not rel_path:
            return
        try:
            p = self._abs(rel_path)
            if p.is_file():
                p.unlink()
        except Exception as e:
            logger.warning("local delete failed %s: %s", rel_path, e)

    def test_connection(self) -> Dict[str, object]:
        try:
            self.require()
            probe = self.root / f".fts_test_{uuid.uuid4().hex[:8]}.txt"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return {
                "ok": True,
                "hint": f"本地存储正常，根目录：{self.root}",
                "root": str(self.root),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
