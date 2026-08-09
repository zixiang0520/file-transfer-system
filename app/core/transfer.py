"""Transfer business logic: multi-file package + one extract code."""
from __future__ import annotations

import io
import re
import secrets
import string
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app import db
from app.config_store import load_config
from app.core import storage as store


class TransferError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def _gen_code(length: int = 6) -> str:
    # avoid ambiguous 0/O/1/I
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _unique_code(length: int) -> str:
    for _ in range(20):
        code = _gen_code(length)
        if not db.get_package_by_code(code):
            return code
    raise TransferError("生成提取码失败，请重试", 500)


def validate_filename(name: str) -> Tuple[bool, str]:
    cfg = load_config()
    up = cfg.get("upload") or {}
    allowed = [x.lower().lstrip(".") for x in (up.get("allowed_extensions") or [])]
    max_mb = float(up.get("max_file_size_mb") or 500)
    pure = Path(name).name
    if not pure or pure in (".", ".."):
        return False, "非法文件名"
    ext = pure.rsplit(".", 1)[-1].lower() if "." in pure else ""
    if allowed and ext not in allowed:
        return False, f"不允许的类型 .{ext or '(无扩展名)'}，允许：{', '.join(allowed[:12])}…"
    return True, pure


def max_bytes() -> int:
    cfg = load_config()
    return int(float((cfg.get("upload") or {}).get("max_file_size_mb") or 500) * 1024 * 1024)


def create_package_with_files(
    *,
    files: Sequence[Tuple[str, bytes, str]],  # (filename, content, content_type)
    expire_hours: Optional[float] = None,
    max_extracts: Optional[int] = None,
    title: str = "",
    uploader: str = "",
    source: str = "web",
) -> Dict[str, Any]:
    cfg = load_config()
    up = cfg.get("upload") or {}
    max_n = int(up.get("max_files_per_package") or 50)
    if not files:
        raise TransferError("请至少上传一个文件")
    if len(files) > max_n:
        raise TransferError(f"单次最多 {max_n} 个文件")

    limit = max_bytes()
    prepared: List[Tuple[str, bytes, str]] = []
    for name, content, ctype in files:
        ok, pure = validate_filename(name)
        if not ok:
            raise TransferError(pure)
        if len(content) > limit:
            raise TransferError(f"{pure} 超过大小限制 {up.get('max_file_size_mb')}MB")
        if len(content) <= 0:
            raise TransferError(f"{pure} 是空文件")
        prepared.append((pure, content, ctype or "application/octet-stream"))

    hours = expire_hours
    if hours is None:
        hours = float(up.get("default_expire_hours") or 72)
    hours = float(hours)
    if hours <= 0:
        hours = float(up.get("default_expire_hours") or 72)
    # 最长有效期：后台可配置（单位：天）
    max_days = float(up.get("max_expire_days") or 90)
    if max_days < 1:
        max_days = 1
    if max_days > 3650:
        max_days = 3650
    max_hours = max_days * 24
    if hours > max_hours:
        raise TransferError(f"有效期不能超过 {int(max_days) if max_days == int(max_days) else max_days} 天")
    hours = min(hours, max_hours)

    # 0 = unlimited
    extracts = int(max_extracts) if max_extracts is not None else int(up.get("default_max_extracts") or 0)
    if extracts < 0:
        extracts = 0
    if extracts > 100000:
        extracts = 100000

    code_len = int(up.get("extract_code_length") or 6)
    code = _unique_code(code_len)
    expire_at = time.time() + hours * 3600
    pkg_id = db.create_package(
        extract_code=code,
        expire_at=expire_at,
        title=title or prepared[0][0],
        uploader=uploader,
        source=source,
        max_extracts=extracts,
    )

    saved = []
    for pure, content, ctype in prepared:
        bio = io.BytesIO(content)
        meta = store.save_file(bio, pure)
        fid = db.add_file(
            package_id=pkg_id,
            original_name=pure,
            stored_name=meta["stored_name"],
            size=int(meta["size"]),
            content_type=ctype,
            storage_backend=meta["backend"],
            storage_path=meta["storage_path"],
            remote_id=meta.get("remote_id") or "",
            sha256=meta.get("sha256") or "",
        )
        saved.append(
            {
                "id": fid,
                "name": pure,
                "size": int(meta["size"]),
                "content_type": ctype,
            }
        )

    return {
        "package_id": pkg_id,
        "extract_code": code,
        "expire_at": expire_at,
        "expire_hours": hours,
        "max_extracts": extracts,
        "files": saved,
        "file_count": len(saved),
        "total_size": sum(x["size"] for x in saved),
    }


def _assert_package_usable(pkg: Dict[str, Any]) -> None:
    if not pkg:
        raise TransferError("提取码无效", 404)
    if pkg.get("status") != "active" or float(pkg["expire_at"]) < time.time():
        raise TransferError("文件已过期或已失效", 410)
    max_e = int(pkg.get("max_extracts") or 0)
    used = int(pkg.get("download_count") or 0)
    if max_e > 0 and used >= max_e:
        # already exhausted — destroy if still around
        try:
            purge_package(int(pkg["id"]))
        except Exception:
            pass
        raise TransferError("提取次数已用尽，文件已销毁", 410)


def _after_extract(pkg_id: int, max_extracts: int, new_count: int) -> None:
    """If limit reached after this extract/download, destroy package."""
    if max_extracts > 0 and new_count >= max_extracts:
        try:
            purge_package(pkg_id)
        except Exception:
            try:
                db.mark_expired(pkg_id)
            except Exception:
                pass


def get_package_public(code: str) -> Dict[str, Any]:
    pkg = db.get_package_by_code(code)
    _assert_package_usable(pkg)
    assert pkg is not None
    files = db.list_files(int(pkg["id"]))
    max_e = int(pkg.get("max_extracts") or 0)
    used = int(pkg.get("download_count") or 0)
    remain = None if max_e <= 0 else max(0, max_e - used)
    return {
        "extract_code": pkg["extract_code"],
        "title": pkg.get("title") or "",
        "created_at": pkg["created_at"],
        "expire_at": pkg["expire_at"],
        "download_count": used,
        "max_extracts": max_e,
        "remaining_extracts": remain,
        "files": [
            {
                "id": f["id"],
                "name": f["original_name"],
                "size": f["size"],
                "content_type": f.get("content_type") or "",
            }
            for f in files
        ],
        "file_count": len(files),
        "total_size": sum(int(f["size"]) for f in files),
    }


def resolve_download(code: str, file_id: int) -> Tuple[Dict[str, Any], Path, bool]:
    """Return (file_meta, path, should_destroy_after)."""
    pkg = db.get_package_by_code(code)
    _assert_package_usable(pkg)
    assert pkg is not None
    f = db.get_file(file_id)
    if not f or int(f["package_id"]) != int(pkg["id"]):
        raise TransferError("文件不存在", 404)
    path = store.open_local(f["storage_path"])
    new_count = db.bump_download(int(pkg["id"]))
    max_e = int(pkg.get("max_extracts") or 0)
    should_destroy = max_e > 0 and new_count >= max_e
    return f, path, should_destroy


def build_zip_for_package(code: str) -> Tuple[bytes, str, bool, int]:
    """Return (zip_bytes, filename, should_destroy, package_id)."""
    pkg = db.get_package_by_code(code)
    _assert_package_usable(pkg)
    assert pkg is not None
    info_code = pkg["extract_code"]
    pkg_id = int(pkg["id"])
    files = db.list_files(pkg_id)
    return _zip_files(pkg, files)


def build_zip_for_selected(
    code: str, file_ids: Sequence[int]
) -> Tuple[bytes, str, bool, int]:
    pkg = db.get_package_by_code(code)
    _assert_package_usable(pkg)
    assert pkg is not None
    pkg_id = int(pkg["id"])
    id_set = {int(x) for x in file_ids}
    if not id_set:
        raise TransferError("请先选择要下载的文件")
    files = [f for f in db.list_files(pkg_id) if int(f["id"]) in id_set]
    if not files:
        raise TransferError("未找到所选文件", 404)
    if len(files) != len(id_set):
        raise TransferError("部分文件不存在或不属于此提取码", 404)
    return _zip_files(pkg, files)


def _zip_files(
    pkg: Dict[str, Any], files: List[Dict[str, Any]]
) -> Tuple[bytes, str, bool, int]:
    info_code = pkg["extract_code"]
    pkg_id = int(pkg["id"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        used_names: Dict[str, int] = {}
        for fmeta in files:
            path = store.open_local(fmeta["storage_path"])
            arc = Path(fmeta["original_name"]).name
            # avoid duplicate names in zip
            if arc in used_names:
                used_names[arc] += 1
                stem = Path(arc).stem
                suf = Path(arc).suffix
                arc = f"{stem}_{used_names[arc]}{suf}"
            else:
                used_names[arc] = 0
            zf.write(path, arcname=arc)
    new_count = db.bump_download(pkg_id)
    max_e = int(pkg.get("max_extracts") or 0)
    should_destroy = max_e > 0 and new_count >= max_e
    if len(files) == 1:
        name = f"{Path(files[0]['original_name']).stem}.zip"
    else:
        name = f"{info_code}_selected.zip"
    return buf.getvalue(), name, should_destroy, pkg_id


def purge_package(package_id: int) -> None:
    result = db.delete_package(package_id) or {"files": []}
    for f in result.get("files") or []:
        store.delete_file(f.get("storage_backend") or "local", f.get("storage_path") or "", f.get("remote_id") or "")


def cleanup_expired() -> int:
    expired = db.list_expired()
    n = 0
    for pkg in expired:
        try:
            purge_package(int(pkg["id"]))
            n += 1
        except Exception:
            try:
                db.mark_expired(int(pkg["id"]))
            except Exception:
                pass
    return n
