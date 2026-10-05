"""Transfer business logic: multi-file package + one extract code (cloud only)."""
from __future__ import annotations

import io
import json
import logging
import re
import secrets
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app import db
from app.config_store import load_config
from app.core import ai_review
from app.core import storage as store

logger = logging.getLogger("fts.transfer")

# 139 并行哈希校验规则（2026-10-05 实测矩阵）：
#   多片上传时，尾片必须等于 partSize（严格整除），否则 complete 必报
#   00010326 文件内容摘要值不匹配——官方默认 100MB 分片也不例外
#   （152.5MB=100MB+52.5MB 尾片实测复现；5/8/10/15/20MB 分片尾片 1~10MB 全挂）。
#   安全模式仅两种：①单片（partSize=size，≤512MB 均实测通过）；
#   ②全部片严格等大（size 整除 partSize，5MB×5 / 10MB×3 / 10MB×25 实测通过）。
# 因此直传分片：≤100MB 单片；>100MB 选「能整除 size 的最大整齐值」；无整齐
# 约数的大文件走单片直传，若客户端网络损坏大块（00010326），由前端自动
# 降级服务器流水线中转兜底。
_PART_CANDIDATES = (
    10 * 1024 * 1024,
    8 * 1024 * 1024,
    5 * 1024 * 1024,
    4 * 1024 * 1024,
    2 * 1024 * 1024,
    1 * 1024 * 1024,
)
_MAX_DIRECT_PARTS = 2000  # 片数上限保护（139 分片数量存在上限，保守取值）


def pick_part_size(size: int) -> int:
    """Choose a direct-upload part size for a 139 create task.

    - size <= 100MB: official default 100MB → single part, no tail-part issue.
    - size > 100MB: largest "tidy" candidate that divides size exactly (all
      parts equal, verified safe against 00010326), within the part-count cap.
    - no tidy divisor: single part of the whole file (partSize=size, verified
      safe); if the client network corrupts the big PUT, the front-end falls
      back to server relay automatically.
    """
    size = int(size)
    if size <= 0:
        return store.PART_SIZE
    if size <= store.PART_SIZE:
        return store.PART_SIZE
    for cand in _PART_CANDIDATES:
        if size % cand == 0 and size // cand <= _MAX_DIRECT_PARTS:
            return cand
    return size  # 单片直传：无尾片，唯一安全的非整除形态


class TransferError(Exception):
    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def _gen_code(length: int = 6) -> str:
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
    files: Sequence[Tuple[str, bytes, str]],
    expire_hours: Optional[float] = None,
    max_extracts: Optional[int] = None,
    title: str = "",
    uploader: str = "",
    source: str = "web",
    client_ip: str = "",
) -> Dict[str, Any]:
    cfg = load_config()
    up = cfg.get("upload") or {}
    st = cfg.get("storage") or {}
    y = st.get("yun139") or {}
    if not y.get("enabled") or not (y.get("authorization") or "").strip():
        raise TransferError("未配置移动云盘：请在后台启用并填写 Authorization", 503)

    ip = (client_ip or "").strip()
    if ip:
        banned = db.is_ip_banned(ip)
        if banned:
            raise TransferError("该地址已被禁止上传", 403)

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
    max_days = float(up.get("max_expire_days") or 90)
    if max_days < 1:
        max_days = 1
    if max_days > 3650:
        max_days = 3650
    max_hours = max_days * 24
    if hours > max_hours:
        raise TransferError(f"有效期不能超过 {int(max_days) if max_days == int(max_days) else max_days} 天")
    hours = min(hours, max_hours)

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
        uploader_ip=ip,
    )

    saved = []
    try:
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
    except store.StorageError as e:
        try:
            purge_package(pkg_id)
        except Exception:
            pass
        raise TransferError(e.message, getattr(e, "code", 502))
    except Exception as e:
        try:
            purge_package(pkg_id)
        except Exception:
            pass
        raise TransferError(f"上传云盘失败: {e}", 502)

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


def init_direct_upload(
    *,
    files_meta: Sequence[Dict[str, Any]],
    expire_hours: Optional[float] = None,
    max_extracts: Optional[int] = None,
    title: str = "",
    uploader: str = "",
    source: str = "web",
    client_ip: str = "",
) -> Dict[str, Any]:
    """Create package + 139 upload tasks; the browser PUTs parts directly to the
    cloud via presigned URLs, then calls complete_direct_upload()."""
    cfg = load_config()
    up = cfg.get("upload") or {}
    st = cfg.get("storage") or {}
    y = st.get("yun139") or {}
    if not y.get("enabled") or not (y.get("authorization") or "").strip():
        raise TransferError("未配置移动云盘：请在后台启用并填写 Authorization", 503)

    ip = (client_ip or "").strip()
    if ip:
        banned = db.is_ip_banned(ip)
        if banned:
            raise TransferError("该地址已被禁止上传", 403)

    max_n = int(up.get("max_files_per_package") or 50)
    if not files_meta:
        raise TransferError("请至少上传一个文件")
    if len(files_meta) > max_n:
        raise TransferError(f"单次最多 {max_n} 个文件")

    limit = max_bytes()
    prepared: List[Dict[str, Any]] = []
    for fm in files_meta:
        name = str(fm.get("name") or "")
        ok, pure = validate_filename(name)
        if not ok:
            raise TransferError(pure)
        size = int(fm.get("size") or 0)
        sha = str(fm.get("sha256") or "").strip().lower()
        if size > limit:
            raise TransferError(f"{pure} 超过大小限制 {up.get('max_file_size_mb')}MB")
        if size <= 0:
            raise TransferError(f"{pure} 是空文件")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise TransferError(f"{pure} 缺少有效的 SHA256（直传需要）")
        if db.is_sha_blacklisted(sha):
            raise TransferError(f"{pure} 已被禁止上传（文件内容列入黑名单）", 403)
        # AI 审核第一道：文件名/标题（未启用或调用失败自动放行）
        ar = ai_review.review_filename(pure, title=title)
        db.add_ai_review(
            file_name=pure, kind="filename", verdict=ar.get("verdict", "skip"),
            reason=ar.get("reason", ""),
        )
        if ar.get("verdict") == "reject":
            raise TransferError(
                f"{pure} 未通过安全审核：{ar.get('reason') or '内容违规'}", 403
            )
        prepared.append(
            {
                "name": pure,
                "size": size,
                "sha256": sha,
                "content_type": str(fm.get("content_type") or "application/octet-stream"),
            }
        )

    logger.info(
        "direct-upload init: pkg files=%s",
        json.dumps(prepared, ensure_ascii=False)[:800],
    )

    hours = expire_hours
    if hours is None:
        hours = float(up.get("default_expire_hours") or 72)
    hours = float(hours)
    if hours <= 0:
        hours = float(up.get("default_expire_hours") or 72)
    max_days = float(up.get("max_expire_days") or 90)
    if max_days < 1:
        max_days = 1
    if max_days > 3650:
        max_days = 3650
    max_hours = max_days * 24
    if hours > max_hours:
        raise TransferError(f"有效期不能超过 {int(max_days) if max_days == int(max_days) else max_days} 天")
    hours = min(hours, max_hours)

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
        title=title or prepared[0]["name"],
        uploader=uploader,
        source=source,
        max_extracts=extracts,
        uploader_ip=ip,
    )

    created_tasks: List[Dict[str, Any]] = []
    try:
        for fm in prepared:
            # 直传分片策略见 pick_part_size：≤100MB 单片；>100MB 选能整除的整齐
            # 分片（139 非默认分片尾片不整除会 00010326）；无整齐约数回退官方 100MB
            task = store.create_upload_task(
                fm["name"], fm["size"], fm["sha256"], part_size=pick_part_size(fm["size"])
            )
            if not task.get("file_id"):
                raise store.StorageError("云盘创建上传任务失败：无 fileId", 502)
            created_tasks.append(
                {
                    "name": fm["name"],
                    "size": fm["size"],
                    "sha256": fm["sha256"],
                    "content_type": fm["content_type"],
                    "file_id": task["file_id"],
                    "upload_id": task["upload_id"],
                    "exist": task["exist"],
                    "rapid": task.get("rapid"),
                    "file_name": task["file_name"],
                    "parts": task["parts"],
                }
            )
    except Exception:
        for t in created_tasks:
            try:
                store.delete_file("yun139", f"yun139://{t['file_id']}", t["file_id"])
            except Exception:
                pass
        try:
            db.delete_package(pkg_id)
        except Exception:
            pass
        raise

    return {
        "package_id": pkg_id,
        "extract_code": code,
        "expire_at": expire_at,
        "expire_hours": hours,
        "max_extracts": extracts,
        "ai_enabled": ai_review.enabled(),
        "ai_max_review_mb": ai_review.max_review_mb() if ai_review.enabled() else 0,
        "files": created_tasks,
    }


def complete_direct_upload(
    *, package_id: int, files: Sequence[Dict[str, Any]]
) -> Dict[str, Any]:
    """Finalize a direct upload: complete 139 tasks and record files in DB."""
    saved: List[Dict[str, Any]] = []
    try:
        logger.info(
            "direct-upload complete: pkg=%s files=%s",
            package_id,
            json.dumps(
                [
                    {
                        "name": f.get("name"),
                        "size": f.get("size"),
                        "sha256": (f.get("sha256") or "")[:16],
                        "file_id": f.get("file_id"),
                        "upload_id": f.get("upload_id"),
                        "exist": f.get("exist"),
                        "rapid": f.get("rapid"),
                    }
                    for f in files
                ],
                ensure_ascii=False,
            ),
        )
        for fm in files:
            file_id = str(fm.get("file_id") or "")
            upload_id = str(fm.get("upload_id") or "")
            sha = str(fm.get("sha256") or "").strip().lower()
            name = str(fm.get("name") or "")
            ctype = str(fm.get("content_type") or "application/octet-stream")
            size = int(fm.get("size") or 0)
            exist = bool(fm.get("exist"))
            rapid = bool(fm.get("rapid"))
            if not file_id:
                raise TransferError("文件缺少 file_id，请重新上传", 400)
            if not exist and not rapid:
                store.complete_upload(file_id, upload_id, sha)
            meta = {
                "backend": "yun139",
                "storage_path": f"yun139://{file_id}",
                "stored_name": str(fm.get("file_name") or name),
                "size": str(size),
                "sha256": sha,
                "remote_id": file_id,
            }
            fid = db.add_file(
                package_id=package_id,
                original_name=name,
                stored_name=meta["stored_name"],
                size=size,
                content_type=ctype,
                storage_backend=meta["backend"],
                storage_path=meta["storage_path"],
                remote_id=meta["remote_id"],
                sha256=meta["sha256"],
            )
            saved.append(
                {
                    "id": fid,
                    "name": name,
                    "size": size,
                    "content_type": ctype,
                }
            )
    except Exception as e:
        try:
            purge_package(package_id)
        except Exception:
            pass
        if isinstance(e, TransferError):
            logger.warning("direct-upload complete failed (TransferError): %s", e)
            raise
        logger.warning("direct-upload complete failed: %s", e)
        raise TransferError(f"完成上传失败: {e}", 502)

    return {
        "package_id": package_id,
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
        try:
            purge_package(int(pkg["id"]))
        except Exception:
            pass
        raise TransferError("提取次数已用尽，文件已销毁", 410)


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


def resolve_download(code: str, file_id: int) -> Tuple[Dict[str, Any], str, bool]:
    """Return (file_meta, cloud_download_url, should_destroy_after)."""
    pkg = db.get_package_by_code(code)
    _assert_package_usable(pkg)
    assert pkg is not None
    f = db.get_file(file_id)
    if not f or int(f["package_id"]) != int(pkg["id"]):
        raise TransferError("文件不存在", 404)
    try:
        url = store.get_download_url(f.get("storage_path") or "", f.get("remote_id") or "")
    except store.StorageError as e:
        raise TransferError(e.message, getattr(e, "code", 502))
    new_count = db.bump_download(int(pkg["id"]))
    max_e = int(pkg.get("max_extracts") or 0)
    should_destroy = max_e > 0 and new_count >= max_e
    return f, url, should_destroy


def build_zip_for_package(code: str) -> Tuple[bytes, str, bool, int]:
    pkg = db.get_package_by_code(code)
    _assert_package_usable(pkg)
    assert pkg is not None
    files = db.list_files(int(pkg["id"]))
    return _zip_files(pkg, files)


def build_zip_for_selected(code: str, file_ids: Sequence[int]) -> Tuple[bytes, str, bool, int]:
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


def _zip_files(pkg: Dict[str, Any], files: List[Dict[str, Any]]) -> Tuple[bytes, str, bool, int]:
    info_code = pkg["extract_code"]
    pkg_id = int(pkg["id"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        used_names: Dict[str, int] = {}
        for fmeta in files:
            try:
                data = store.open_file_bytes(fmeta.get("storage_path") or "", fmeta.get("remote_id") or "")
            except store.StorageError as e:
                raise TransferError(e.message, getattr(e, "code", 502))
            arc = Path(fmeta["original_name"]).name
            if arc in used_names:
                used_names[arc] += 1
                stem = Path(arc).stem
                suf = Path(arc).suffix
                arc = f"{stem}_{used_names[arc]}{suf}"
            else:
                used_names[arc] = 0
            zf.writestr(arc, data)
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
        store.delete_file(
            f.get("storage_backend") or "yun139",
            f.get("storage_path") or "",
            f.get("remote_id") or "",
        )


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
