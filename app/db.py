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
            # migrate older DBs missing max_extracts
            cols = {r[1] for r in con.execute("PRAGMA table_info(packages)").fetchall()}
            if "max_extracts" not in cols:
                con.execute(
                    "ALTER TABLE packages ADD COLUMN max_extracts INTEGER DEFAULT 0"
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
) -> int:
    with _lock:
        con = _conn()
        try:
            cur = con.execute(
                """
                INSERT INTO packages (
                    extract_code, title, created_at, expire_at, uploader, source, max_extracts
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extract_code,
                    title,
                    time.time(),
                    expire_at,
                    uploader,
                    source,
                    int(max_extracts or 0),
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
