#!/usr/bin/env python3
"""One-shot schema migrate: packages.uploader_ip + banned_ips."""
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "transfers.db"


def main() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(packages)")}
        if "uploader_ip" not in cols:
            con.execute("ALTER TABLE packages ADD COLUMN uploader_ip TEXT DEFAULT ''")
            print("added packages.uploader_ip")
        else:
            print("uploader_ip already present")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS banned_ips (
                ip TEXT PRIMARY KEY,
                reason TEXT DEFAULT '',
                created_at REAL NOT NULL,
                created_by TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_pkg_ip ON packages(uploader_ip);
            """
        )
        con.commit()
        print("ok", [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")])
    finally:
        con.close()


if __name__ == "__main__":
    main()
