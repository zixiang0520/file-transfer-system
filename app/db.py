"""SQLite models for transfer packages (one code → many files)."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config_store import DB_PATH, DATA_DIR

_lock = threading.RLock()


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock:
        con = _conn()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS packages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extract_code TEXT NOT NULL UNIQUE,
                    title TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    expire_at REAL NOT NULL,
                    uploader TEXT DEFAULT '',
                    source TEXT DEFAULT 'web',
                    download_count INTEGER DEFAULT 0,
                    max_extracts INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    package_id INTEGER NOT NULL,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    content_type TEXT DEFAULT '',
                    storage_backend TEXT DEFAULT 'local',
                    storage_path TEXT NOT NULL,
                    remote_id TEXT DEFAULT '',
                    sha256 TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    FOREIGN KEY(package_id) REFERENCES packages(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_pkg_code ON packages(extract_code);
                CREATE INDEX IF NOT EXISTS idx_pkg_expire ON packages(expire_at);
                CREATE INDEX IF NOT EXISTS idx_files_pkg ON files(package_id);
                """
            )
            # migrate older DBs
            cols = {r[1] for r in con.execute("PRAGMA table_info(packages)").fetchall()}
            if "max_extracts" not in cols:
                con.execute(
                    "ALTER TABLE packages ADD COLUMN max_extracts INTEGER DEFAULT 0"
                )
            if "uploader_ip" not in cols:
                con.execute(
                    "ALTER TABLE packages ADD COLUMN uploader_ip TEXT DEFAULT ''"
                )
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS banned_ips (
                    ip TEXT PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    created_by TEXT DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_pkg_ip ON packages(uploader_ip);
                CREATE TABLE IF NOT EXISTS sha_blacklist (
                    sha256 TEXT PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    created_by TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extract_code TEXT NOT NULL,
                    package_id INTEGER DEFAULT 0,
                    file_summary TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    reporter_ip TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    status TEXT DEFAULT 'open',
                    action_taken TEXT DEFAULT '',
                    resolved_by TEXT DEFAULT '',
                    resolved_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_reports_code ON reports(extract_code);
                CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
                """
            )
            con.commit()
        finally:
            con.close()


def create_package(
    *,
    extract_code: str,
    expire_at: float,
    title: str = "",
    uploader: str = "",
    source: str = "web",
    max_extracts: int = 0,
    uploader_ip: str = "",
) -> int:
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                """
                INSERT INTO packages (
                    extract_code, title, created_at, expire_at, uploader, source,
                    max_extracts, uploader_ip
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extract_code,
                    title,
                    time.time(),
                    expire_at,
                    uploader,
                    source,
                    int(max_extracts or 0),
                    (uploader_ip or "").strip(),
                ),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()


def add_file(
    *,
    package_id: int,
    original_name: str,
    stored_name: str,
    size: int,
    content_type: str,
    storage_backend: str,
    storage_path: str,
    remote_id: str = "",
    sha256: str = "",
) -> int:
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                """
                INSERT INTO files (
                    package_id, original_name, stored_name, size, content_type,
                    storage_backend, storage_path, remote_id, sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    package_id,
                    original_name,
                    stored_name,
                    size,
                    content_type,
                    storage_backend,
                    storage_path,
                    remote_id,
                    sha256,
                    time.time(),
                ),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()


def get_package_by_code(code: str) -> Optional[Dict[str, Any]]:
    code = (code or "").strip().upper()
    with _lock:
        con = _conn()
        try:
            row = con.execute(
                "SELECT * FROM packages WHERE extract_code = ?", (code,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def get_package(pkg_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        con = _conn()
        try:
            row = con.execute("SELECT * FROM packages WHERE id = ?", (pkg_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def list_files(package_id: int) -> List[Dict[str, Any]]:
    with _lock:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT * FROM files WHERE package_id = ? ORDER BY id", (package_id,)
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def get_file(file_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        con = _conn()
        try:
            row = con.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def bump_download(package_id: int) -> int:
    """Increment download/extract count; return new count."""
    with _lock:
        con = _conn()
        try:
            con.execute(
                "UPDATE packages SET download_count = download_count + 1 WHERE id = ?",
                (package_id,),
            )
            con.commit()
            row = con.execute(
                "SELECT download_count FROM packages WHERE id = ?", (package_id,)
            ).fetchone()
            return int(row["download_count"]) if row else 0
        finally:
            con.close()


def list_packages(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    with _lock:
        con = _conn()
        try:
            rows = con.execute(
                """
                SELECT p.*, (SELECT COUNT(*) FROM files f WHERE f.package_id = p.id) AS file_count,
                       (SELECT COALESCE(SUM(size),0) FROM files f WHERE f.package_id = p.id) AS total_size
                FROM packages p
                ORDER BY p.id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def delete_package(package_id: int) -> Optional[Dict[str, Any]]:
    """Delete package row + return file rows for storage cleanup."""
    with _lock:
        con = _conn()
        try:
            files = [dict(r) for r in con.execute(
                "SELECT * FROM files WHERE package_id = ?", (package_id,)
            ).fetchall()]
            con.execute("DELETE FROM files WHERE package_id = ?", (package_id,))
            con.execute("DELETE FROM packages WHERE id = ?", (package_id,))
            con.commit()
            return {"files": files}
        finally:
            con.close()


def list_expired(now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = now or time.time()
    with _lock:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT * FROM packages WHERE status = 'active' AND expire_at < ?",
                (now,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def mark_expired(package_id: int) -> None:
    with _lock:
        con = _conn()
        try:
            con.execute(
                "UPDATE packages SET status = 'expired' WHERE id = ?", (package_id,)
            )
            con.commit()
        finally:
            con.close()


def is_ip_banned(ip: str) -> Optional[Dict[str, Any]]:
    ip = (ip or "").strip()
    if not ip:
        return None
    with _lock:
        con = _conn()
        try:
            row = con.execute("SELECT * FROM banned_ips WHERE ip = ?", (ip,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def ban_ip(ip: str, reason: str = "", created_by: str = "") -> Dict[str, Any]:
    ip = (ip or "").strip()
    if not ip:
        raise ValueError("empty ip")
    now = time.time()
    with _lock:
        con = _conn()
        try:
            con.execute(
                """
                INSERT INTO banned_ips (ip, reason, created_at, created_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ip) DO UPDATE SET
                    reason = excluded.reason,
                    created_at = excluded.created_at,
                    created_by = excluded.created_by
                """,
                (ip, (reason or "").strip(), now, (created_by or "").strip()),
            )
            con.commit()
            row = con.execute("SELECT * FROM banned_ips WHERE ip = ?", (ip,)).fetchone()
            return dict(row) if row else {"ip": ip, "reason": reason, "created_at": now}
        finally:
            con.close()


def unban_ip(ip: str) -> bool:
    ip = (ip or "").strip()
    if not ip:
        return False
    with _lock:
        con = _conn()
        try:
            cur = con.execute("DELETE FROM banned_ips WHERE ip = ?", (ip,))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def list_banned_ips() -> List[Dict[str, Any]]:
    with _lock:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT * FROM banned_ips ORDER BY created_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def count_packages_by_ip(ip: str) -> int:
    ip = (ip or "").strip()
    if not ip:
        return 0
    with _lock:
        con = _conn()
        try:
            row = con.execute(
                "SELECT COUNT(*) AS n FROM packages WHERE uploader_ip = ?", (ip,)
            ).fetchone()
            return int(row["n"]) if row else 0
        finally:
            con.close()


# ---------- SHA-256 黑名单 ----------

def is_sha_blacklisted(sha256: str) -> Optional[Dict[str, Any]]:
    sha256 = (sha256 or "").strip().lower()
    if not sha256:
        return None
    with _lock:
        con = _conn()
        try:
            row = con.execute(
                "SELECT * FROM sha_blacklist WHERE sha256 = ?", (sha256,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def add_sha_blacklist(sha256: str, reason: str = "", created_by: str = "") -> Dict[str, Any]:
    sha256 = (sha256 or "").strip().lower()
    now = time.time()
    with _lock:
        con = _conn()
        try:
            con.execute(
                "INSERT OR IGNORE INTO sha_blacklist (sha256, reason, created_at, created_by) VALUES (?, ?, ?, ?)",
                (sha256, reason or "", now, created_by or ""),
            )
            con.commit()
            row = con.execute(
                "SELECT * FROM sha_blacklist WHERE sha256 = ?", (sha256,)
            ).fetchone()
            return dict(row) if row else {}
        finally:
            con.close()


def list_sha_blacklist() -> List[Dict[str, Any]]:
    with _lock:
        con = _conn()
        try:
            rows = con.execute(
                "SELECT * FROM sha_blacklist ORDER BY created_at DESC LIMIT 500"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def delete_sha_blacklist(sha256: str) -> bool:
    sha256 = (sha256 or "").strip().lower()
    with _lock:
        con = _conn()
        try:
            cur = con.execute("DELETE FROM sha_blacklist WHERE sha256 = ?", (sha256,))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


# ---------- 举报 ----------

def add_report(
    *,
    extract_code: str,
    package_id: int = 0,
    file_summary: str = "",
    reason: str = "",
    reporter_ip: str = "",
) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                """INSERT INTO reports
                   (extract_code, package_id, file_summary, reason, reporter_ip, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (extract_code, int(package_id or 0), file_summary or "", reason or "",
                 reporter_ip or "", now),
            )
            con.commit()
            row = con.execute("SELECT * FROM reports WHERE id = ?", (cur.lastrowid,)).fetchone()
            return dict(row) if row else {}
        finally:
            con.close()


def has_recent_report(extract_code: str, reporter_ip: str, hours: int = 24) -> bool:
    extract_code = (extract_code or "").strip().upper()
    reporter_ip = (reporter_ip or "").strip()
    if not extract_code or not reporter_ip:
        return False
    since = time.time() - hours * 3600
    with _lock:
        con = _conn()
        try:
            row = con.execute(
                """SELECT 1 FROM reports
                   WHERE extract_code = ? AND reporter_ip = ? AND created_at >= ?
                   LIMIT 1""",
                (extract_code, reporter_ip, since),
            ).fetchone()
            return bool(row)
        finally:
            con.close()


def list_reports(*, open_only: bool = False, limit: int = 200) -> List[Dict[str, Any]]:
    q = "SELECT * FROM reports"
    if open_only:
        q += " WHERE status = 'open'"
    q += " ORDER BY created_at DESC LIMIT ?"
    with _lock:
        con = _conn()
        try:
            rows = con.execute(q, (int(limit),)).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()


def get_report(report_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        con = _conn()
        try:
            row = con.execute("SELECT * FROM reports WHERE id = ?", (int(report_id),)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()


def resolve_report(report_id: int, *, action_taken: str, resolved_by: str) -> bool:
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                """UPDATE reports SET status='resolved', action_taken=?, resolved_by=?, resolved_at=?
                   WHERE id=?""",
                (action_taken or "", resolved_by or "", time.time(), int(report_id)),
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()


def delete_report(report_id: int) -> bool:
    with _lock:
        con = _conn()
        try:
            cur = con.execute("DELETE FROM reports WHERE id = ?", (int(report_id),))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()
