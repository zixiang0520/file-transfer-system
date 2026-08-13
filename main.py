"""File Transfer System - FastAPI entry."""
from __future__ import annotations

import ipaddress
import logging
import threading
import time
from pathlib import Path
from typing import List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app.config_store import (
    load_config,
    public_config_view,
    save_config,
    set_admin_password,
    verify_admin,
)
from app.core.storage import Yun139Client
from app.core.transfer import (
    TransferError,
    build_zip_for_package,
    build_zip_for_selected,
    cleanup_expired,
    create_package_with_files,
    get_package_public,
    purge_package,
    resolve_download,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(ROOT / "app" / "web" / "templates"))
STATIC = ROOT / "app" / "web" / "static"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fts")

app = FastAPI(title="文件流转系统", docs_url=None, redoc_url=None)

cfg0 = load_config()
app.add_middleware(
    SessionMiddleware,
    secret_key=cfg0.get("session_secret") or "fts-dev-secret",
    max_age=7 * 24 * 3600,
)

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


def require_admin(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(status_code=401, detail="未登录")


def _normalize_ip(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("[") and "]" in raw:
        raw = raw[1 : raw.index("]")]
    if raw.count(":") == 1 and "." in raw:
        raw = raw.rsplit(":", 1)[0]
    try:
        ip = ipaddress.ip_address(raw)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ip)
    except ValueError:
        return ""


def _is_private_or_local(ip: str) -> bool:
    try:
        obj = ipaddress.ip_address(ip)
        return bool(obj.is_private or obj.is_loopback or obj.is_link_local)
    except ValueError:
        return True


def get_client_ip(request: Request) -> str:
    """Peer address; if peer is loopback/private, honor X-Real-IP / X-Forwarded-For."""
    peer = ""
    if request.client and request.client.host:
        peer = _normalize_ip(request.client.host)
    xri = _normalize_ip(request.headers.get("x-real-ip") or "")
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0]
    xff = _normalize_ip(xff)
    if peer and not _is_private_or_local(peer):
        return peer
    return xri or xff or peer


def _validate_ban_ip(raw: str) -> str:
    ip = _normalize_ip(raw)
    if not ip:
        raise HTTPException(status_code=400, detail="IP 地址无效")
    return ip


@app.on_event("startup")
def on_startup():
    db.init_db()
    load_config()

    def _cleaner():
        while True:
            try:
                n = cleanup_expired()
                if n:
                    logger.info("cleaned %s expired packages", n)
            except Exception as e:
                logger.warning("cleanup error: %s", e)
            time.sleep(300)

    t = threading.Thread(target=_cleaner, daemon=True, name="fts-cleaner")
    t.start()


@app.get("/health")
def health():
    return {"ok": True, "service": "file-transfer-system"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    cfg = load_config()
    up = cfg.get("upload") or {}
    max_days = float(up.get("max_expire_days") or 90)
    if max_days == int(max_days):
        max_days_disp = int(max_days)
    else:
        max_days_disp = max_days
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "site_name": (cfg.get("site") or {}).get("name") or "文件流转系统",
            "max_expire_days": max_days_disp,
        },
    )


@app.get("/extract", response_class=HTMLResponse)
def extract_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "extract.html", {})


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    return TEMPLATES.TemplateResponse(request, "admin.html", {})


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not verify_admin(username, password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    request.session["admin"] = True
    request.session["admin_user"] = username
    return {"ok": True}


@app.post("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/config")
def api_config(_: None = Depends(require_admin)):
    return public_config_view()


@app.post("/api/config")
async def api_config_save(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    cfg = load_config()

    def keep_secret(section: str, key: str, new_val):
        if new_val is None:
            return
        if isinstance(new_val, str) and (new_val == "" or "…" in new_val or new_val == "****"):
            return  # keep existing
        cfg.setdefault(section, {})[key] = new_val

    if "site" in body and isinstance(body["site"], dict):
        cfg["site"].update({k: v for k, v in body["site"].items() if v is not None})
    if "upload" in body and isinstance(body["upload"], dict):
        u = body["upload"]
        for k in (
            "max_file_size_mb",
            "max_files_per_package",
            "default_expire_hours",
            "max_expire_days",
            "extract_code_length",
            "default_max_extracts",
        ):
            if k in u and u[k] is not None:
                val = u[k]
                if k == "max_expire_days":
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        continue
                    if val < 1:
                        val = 1
                    if val > 3650:
                        val = 3650
                cfg["upload"][k] = val
        if "allowed_extensions" in u and isinstance(u["allowed_extensions"], list):
            cfg["upload"]["allowed_extensions"] = [
                str(x).lower().lstrip(".") for x in u["allowed_extensions"] if str(x).strip()
            ]
        if "expire_options_hours" in u and isinstance(u["expire_options_hours"], list):
            cfg["upload"]["expire_options_hours"] = [float(x) for x in u["expire_options_hours"]]

    if "storage" in body and isinstance(body["storage"], dict):
        st = body["storage"]
        if "backend" in st and st["backend"]:
            cfg["storage"]["backend"] = st["backend"]
        if "yun139" in st and isinstance(st["yun139"], dict):
            y = st["yun139"]
            if "enabled" in y:
                cfg["storage"]["yun139"]["enabled"] = bool(y["enabled"])
            for k in ("root_folder_id", "account_hint", "username"):
                if k in y and y[k] is not None:
                    cfg["storage"]["yun139"][k] = y[k]
            # secrets: empty / masked => keep
            for k in ("authorization", "mail_cookies", "password"):
                if k in y:
                    val = y[k]
                    if not isinstance(val, str):
                        continue
                    val = val.strip()
                    # empty / masked => keep existing
                    if not val or "…" in val or "..." in val or val == "****" or set(val) <= {"*"}:
                        continue
                    if k == "authorization" and val.lower().startswith("basic "):
                        val = val[6:].strip()
                    cfg["storage"]["yun139"][k] = val

    if "qq" in body and isinstance(body["qq"], dict):
        q = body["qq"]
        if "enabled" in q:
            cfg["qq"]["enabled"] = bool(q["enabled"])
        if "app_id" in q and q["app_id"] is not None:
            cfg["qq"]["app_id"] = q["app_id"]
        if "client_secret" in q:
            val = q["client_secret"]
            if isinstance(val, str) and val and "…" not in val and val != "****":
                cfg["qq"]["client_secret"] = val

    if "proxy" in body and isinstance(body["proxy"], dict):
        cfg["proxy"].update({k: v for k, v in body["proxy"].items() if v is not None})

    # 管理员账号 / 密码
    if "admin" in body and isinstance(body["admin"], dict):
        new_user = (body["admin"].get("username") or "").strip()
        if new_user:
            if len(new_user) > 64:
                raise HTTPException(status_code=400, detail="用户名过长")
            cfg["admin"]["username"] = new_user

    if body.get("new_password"):
        pw = str(body["new_password"])
        if len(pw) < 6:
            raise HTTPException(status_code=400, detail="新密码至少 6 位")
        set_admin_password(pw)
        cfg = load_config()  # reload after password change path
        # re-apply username if just set above before password rewrite
        if "admin" in body and isinstance(body["admin"], dict):
            new_user = (body["admin"].get("username") or "").strip()
            if new_user:
                cfg["admin"]["username"] = new_user

    save_config(cfg)
    return {"ok": True, "config": public_config_view()}


@app.post("/api/upload")
async def api_upload(
    request: Request,
    files: List[UploadFile] = File(...),
    expire_hours: Optional[float] = Form(None),
    max_extracts: Optional[int] = Form(None),
    title: str = Form(""),
):
    try:
        payload = []
        for f in files:
            content = await f.read()
            payload.append((f.filename or "unnamed", content, f.content_type or "application/octet-stream"))
        result = create_package_with_files(
            files=payload,
            expire_hours=expire_hours,
            max_extracts=max_extracts,
            title=title,
            source="web",
            client_ip=get_client_ip(request),
        )
        cfg = load_config()
        base = (cfg.get("site") or {}).get("public_base_url") or ""
        result["extract_url"] = f"{base.rstrip('/')}/extract?code={result['extract_code']}" if base else f"/extract?code={result['extract_code']}"
        return result
    except TransferError as e:
        return JSONResponse({"error": e.message}, status_code=e.code)
    except Exception as e:
        logger.exception("upload failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/package/{code}")
def api_package(code: str):
    try:
        return get_package_public(code)
    except TransferError as e:
        return JSONResponse({"error": e.message}, status_code=e.code)


@app.get("/api/download/{code}/{file_id}")
def api_download(code: str, file_id: int):
    """Redirect to 移动云盘 download URL (no local file)."""
    try:
        from fastapi.responses import RedirectResponse

        fmeta, url, should_destroy = resolve_download(code, file_id)
        bg = None
        if should_destroy:
            pid = int(fmeta["package_id"])

            def _destroy():
                try:
                    purge_package(pid)
                except Exception:
                    pass

            bg = BackgroundTask(_destroy)
        resp = RedirectResponse(url=url, status_code=302)
        if bg is not None:
            resp.background = bg
        return resp
    except TransferError as e:
        return JSONResponse({"error": e.message}, status_code=e.code)


@app.get("/api/download-all/{code}")
def api_download_all(code: str):
    try:
        data, name, should_destroy, pkg_id = build_zip_for_package(code)
        bg = None
        if should_destroy:
            def _destroy():
                try:
                    purge_package(pkg_id)
                except Exception:
                    pass

            bg = BackgroundTask(_destroy)
        return StreamingResponse(
            iter([data]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
            background=bg,
        )
    except TransferError as e:
        return JSONResponse({"error": e.message}, status_code=e.code)


@app.get("/api/download-selected/{code}")
def api_download_selected(code: str, ids: str = ""):
    """ids: comma-separated file ids, e.g. 1,2,3"""
    try:
        raw = [x.strip() for x in (ids or "").split(",") if x.strip()]
        if not raw:
            return JSONResponse({"error": "请先选择要下载的文件"}, status_code=400)
        file_ids = [int(x) for x in raw]
        data, name, should_destroy, pkg_id = build_zip_for_selected(code, file_ids)
        bg = None
        if should_destroy:
            def _destroy():
                try:
                    purge_package(pkg_id)
                except Exception:
                    pass

            bg = BackgroundTask(_destroy)
        return StreamingResponse(
            iter([data]),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
            background=bg,
        )
    except ValueError:
        return JSONResponse({"error": "文件 ID 无效"}, status_code=400)
    except TransferError as e:
        return JSONResponse({"error": e.message}, status_code=e.code)


@app.get("/api/packages")
def api_packages(limit: int = 100, offset: int = 0, _: None = Depends(require_admin)):
    rows = db.list_packages(limit=limit, offset=offset)
    banned = {b["ip"] for b in db.list_banned_ips()}
    out = []
    for r in rows:
        ip = (r.get("uploader_ip") or "").strip()
        out.append(
            {
                **r,
                "ip_banned": bool(ip and ip in banned),
                "files": [
                    {"id": f["id"], "name": f["original_name"], "size": f["size"]}
                    for f in db.list_files(int(r["id"]))
                ],
            }
        )
    return {"items": out}


@app.delete("/api/packages/{package_id}")
def api_delete_package(package_id: int, _: None = Depends(require_admin)):
    purge_package(package_id)
    return {"ok": True}


@app.post("/api/packages/{package_id}/ban-ip")
async def api_ban_package_ip(
    package_id: int, request: Request, _: None = Depends(require_admin)
):
    pkg = db.get_package(package_id)
    if not pkg:
        raise HTTPException(status_code=404, detail="包裹不存在")
    ip = _validate_ban_ip(pkg.get("uploader_ip") or "")
    body = {}
    try:
        body = await request.json()
        if not isinstance(body, dict):
            body = {}
    except Exception:
        body = {}
    reason = (body.get("reason") or "").strip() or f"包裹 {pkg.get('extract_code')}"
    who = request.session.get("admin_user") or "admin"
    rec = db.ban_ip(ip, reason=reason, created_by=who)
    deleted = False
    if body.get("delete_package"):
        purge_package(package_id)
        deleted = True
    return {
        "ok": True,
        "ip": rec.get("ip") or ip,
        "reason": rec.get("reason") or reason,
        "deleted_package": deleted,
        "upload_count": db.count_packages_by_ip(ip),
    }


@app.get("/api/banned-ips")
def api_list_banned(_: None = Depends(require_admin)):
    items = []
    for rec in db.list_banned_ips():
        items.append({**rec, "upload_count": db.count_packages_by_ip(rec["ip"])})
    return {"items": items}


@app.post("/api/banned-ips")
async def api_ban_ip(request: Request, _: None = Depends(require_admin)):
    body = await request.json()
    ip = _validate_ban_ip((body or {}).get("ip") or "")
    reason = ((body or {}).get("reason") or "").strip()
    who = request.session.get("admin_user") or "admin"
    rec = db.ban_ip(ip, reason=reason, created_by=who)
    return {"ok": True, "item": rec}


@app.delete("/api/banned-ips")
def api_unban_ip(ip: str, _: None = Depends(require_admin)):
    ip = _validate_ban_ip(ip)
    ok = db.unban_ip(ip)
    if not ok:
        raise HTTPException(status_code=404, detail="该 IP 不在封禁名单")
    return {"ok": True, "ip": ip}


@app.post("/api/cleanup")
def api_cleanup(_: None = Depends(require_admin)):
    n = cleanup_expired()
    return {"ok": True, "cleaned": n}


@app.post("/api/yun139/test")
def api_yun139_test(_: None = Depends(require_admin)):
    return Yun139Client().test_connection()


# ---------- QQ bot API (token simple) ----------
@app.post("/api/bot/upload")
async def bot_upload(
    request: Request,
    files: List[UploadFile] = File(...),
    expire_hours: Optional[float] = Form(None),
    max_extracts: Optional[int] = Form(None),
    title: str = Form(""),
    token: str = Form(""),
):
    """QQ 机器人上传接口：multipart + token（默认与 admin 密码同策略，后续可独立 bot token）"""
    cfg = load_config()
    # simple: require session admin OR qq enabled with shared secret header
    auth = request.headers.get("X-Bot-Token") or token
    if not request.session.get("admin"):
        # accept client_secret as bot token if set
        secret = (cfg.get("qq") or {}).get("client_secret") or ""
        if not secret or auth != secret:
            raise HTTPException(status_code=401, detail="bot token invalid")
    try:
        payload = []
        for f in files:
            content = await f.read()
            payload.append((f.filename or "unnamed", content, f.content_type or "application/octet-stream"))
        result = create_package_with_files(
            files=payload,
            expire_hours=expire_hours,
            max_extracts=max_extracts,
            title=title,
            source="qq",
            client_ip=get_client_ip(request),
        )
        return result
    except TransferError as e:
        return JSONResponse({"error": e.message}, status_code=e.code)


@app.get("/api/bot/package/{code}")
def bot_package(code: str, request: Request, token: str = ""):
    cfg = load_config()
    auth = request.headers.get("X-Bot-Token") or token
    secret = (cfg.get("qq") or {}).get("client_secret") or ""
    if not request.session.get("admin"):
        if not secret or auth != secret:
            raise HTTPException(status_code=401, detail="bot token invalid")
    try:
        return get_package_public(code)
    except TransferError as e:
        return JSONResponse({"error": e.message}, status_code=e.code)


def main():
    import argparse
    import os
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("FTS_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("FTS_PORT", "8790")))
    args = p.parse_args()
    uvicorn.run("main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
