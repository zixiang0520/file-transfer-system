"""Config store: data/config.json"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STORAGE_DIR = ROOT / "storage"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "transfers.db"
_lock = threading.RLock()


def _hash_password(password: str) -> str:
    iterations = 200_000
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2${iterations}${salt.hex()}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _, iters_s, salt_hex, dig = stored.split("$", 3)
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iters_s)
        )
        return hmac.compare_digest(dk.hex(), dig)
    except Exception:
        return False


DEFAULTS: Dict[str, Any] = {
    "admin": {"username": "admin", "password_hash": ""},
    "session_secret": "",
    "site": {
        "name": "文件流转系统",
        "public_base_url": "",  # e.g. http://36.140.147.210:8790
        "theme": "aurora",
    },
    "upload": {
        "max_file_size_mb": 500,
        "max_files_per_package": 50,
        "allowed_extensions": [
            "jpg", "jpeg", "png", "gif", "webp", "bmp",
            "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "csv",
            "zip", "rar", "7z", "tar", "gz",
            "mp4", "mkv", "mp3", "wav", "flac",
            "apk", "ipa", "dmg", "exe", "iso",
        ],
        "default_expire_hours": 72,
        "max_expire_days": 90,  # 最长有效期（天），前台/上传不可超过
        "expire_options_hours": [1, 6, 12, 24, 72, 168, 720],  # up to 30d
        "extract_code_length": 6,
        "default_max_extracts": 0,
    },
    "storage": {
        # local | yun139
        "backend": "local",
        "local_root": str(STORAGE_DIR),
        "yun139": {
            "enabled": False,
            # Authorization after Basic (no "Basic " prefix)
            "authorization": "",
            "root_folder_id": "/",
            "account_hint": "",
            # optional future: mail cookies + password login
            "mail_cookies": "",
            "username": "",
            "password": "",
        },
    },
    "qq": {
        "enabled": False,
        "app_id": "",
        "client_secret": "",
    },
    "proxy": {
        "enabled": False,
        "all": "",
        "http": "",
        "https": "",
    },
}


def _ensure(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(DEFAULTS)

    def merge(dst: Dict[str, Any], src: Dict[str, Any]) -> None:
        for k, v in (src or {}).items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = v

    merge(out, cfg or {})
    if not out.get("session_secret"):
        out["session_secret"] = secrets.token_hex(32)
    if not out["admin"].get("password_hash"):
        out["admin"]["password_hash"] = _hash_password("admin123")
    return out


def load_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        if not CONFIG_PATH.exists():
            cfg = _ensure({})
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            return cfg
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = _ensure(raw)
        save_config(cfg)
        return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)


def verify_admin(username: str, password: str) -> bool:
    cfg = load_config()
    if username != cfg["admin"]["username"]:
        return False
    return _verify_password(password, cfg["admin"]["password_hash"])


def set_admin_password(pw: str) -> None:
    cfg = load_config()
    cfg["admin"]["password_hash"] = _hash_password(pw)
    save_config(cfg)


def public_config_view() -> Dict[str, Any]:
    cfg = deepcopy(load_config())
    cfg["admin"].pop("password_hash", None)
    cfg.pop("session_secret", None)

    def mask(v: str) -> str:
        if not v:
            return ""
        if len(v) <= 8:
            return "*" * len(v)
        return v[:4] + "…" + v[-4:]

    y = cfg.get("storage", {}).get("yun139", {})
    if y.get("authorization"):
        y["authorization_set"] = True
        y["authorization"] = mask(y["authorization"])
    else:
        y["authorization_set"] = False
    if y.get("password"):
        y["password_set"] = True
        y["password"] = "****"
    else:
        y["password_set"] = False
    if y.get("mail_cookies"):
        y["mail_cookies_set"] = True
        y["mail_cookies"] = mask(y["mail_cookies"])
    else:
        y["mail_cookies_set"] = False

    qq = cfg.get("qq", {})
    if qq.get("client_secret"):
        qq["client_secret_set"] = True
        qq["client_secret"] = mask(qq["client_secret"])
    else:
        qq["client_secret_set"] = False
    return cfg


def proxy_dict(cfg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    cfg = cfg or load_config()
    p = cfg.get("proxy") or {}
    if not p.get("enabled"):
        return None
    all_p = (p.get("all") or "").strip()
    http_p = (p.get("http") or all_p or "").strip()
    https_p = (p.get("https") or all_p or http_p or "").strip()
    out: Dict[str, str] = {}
    if http_p:
        out["http://"] = http_p
    if https_p:
        out["https://"] = https_p
    return out or None
