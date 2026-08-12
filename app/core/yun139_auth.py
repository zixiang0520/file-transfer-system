"""OpenList-compatible 139 personal cloud auth:
- refresh Authorization via authTokenRefresh.do
- password login fallback: MailCookies + Username + Password
  (mail.10086.cn login → getArtifact → yun.139 thirdlogin)

Aligned with OpenList drivers/139/util.go (loginWithPassword / refreshToken).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from typing import Dict, Optional, Tuple
from urllib.parse import quote, urlencode

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger("fts.yun139_auth")

# From OpenList drivers/139/util.go
KEY_HEX_1 = "73634235495062495331515373756c734e7253306c673d3d"
KEY_HEX_2 = "7150714477323633586746674c337538"
CLIENTKEY_DECRYPT = "l3TryM&Q+X7@dzwk)qP"


def _sha1_hex(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    n = block_size - (len(data) % block_size)
    return data + bytes([n] * n)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("empty pkcs7 data")
    n = data[-1]
    if n < 1 or n > 16 or n > len(data):
        raise ValueError("invalid pkcs7 padding")
    return data[:-n]


def _aes_cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    padded = _pkcs7_pad(plaintext, 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    dec = cipher.decryptor()
    return _pkcs7_unpad(dec.update(ciphertext) + dec.finalize())


def _aes_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    if len(ciphertext) % 16 != 0:
        raise ValueError("AES ECB ciphertext not multiple of block size")
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    dec = cipher.decryptor()
    return _pkcs7_unpad(dec.update(ciphertext) + dec.finalize())


def _sorted_json_stringify(obj) -> str:
    """Match OpenList sortedJsonStringify for request body encryption."""
    if obj is None:
        return "null"
    if isinstance(obj, str):
        try:
            return _sorted_json_stringify(json.loads(obj))
        except Exception:
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        # avoid float for ints
        if isinstance(obj, float) and obj.is_integer():
            return str(int(obj))
        return str(obj)
    if isinstance(obj, list):
        return "[" + ",".join(_sorted_json_stringify(x) for x in obj) + "]"
    if isinstance(obj, dict):
        parts = []
        for k in sorted(obj.keys()):
            key_s = json.dumps(str(k), ensure_ascii=False, separators=(",", ":"))
            parts.append(f"{key_s}:{_sorted_json_stringify(obj[k])}")
        return "{" + ",".join(parts) + "}"
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _proxy_kw() -> dict:
    try:
        from app.config_store import proxy_dict

        px = proxy_dict() or {}
        one = (px.get("https://") or px.get("http://") or "").strip()
        if one:
            return {"proxy": one}
    except Exception:
        pass
    return {}


def _http_client(**extra) -> httpx.Client:
    kw = {"timeout": 60.0, "follow_redirects": False}
    kw.update(_proxy_kw())
    kw.update(extra)
    return httpx.Client(**kw)


def parse_auth_parts(authorization: str) -> Optional[Tuple[str, str, str]]:
    """Return (prefix, account, token_part) from base64 Authorization (no Basic)."""
    auth = (authorization or "").strip()
    if auth.lower().startswith("basic "):
        auth = auth[6:].strip()
    if not auth:
        return None
    try:
        raw = base64.b64decode(auth).decode("utf-8", errors="replace")
    except Exception:
        return None
    parts = raw.split(":", 2)
    if len(parts) < 3:
        return None
    return parts[0], parts[1], parts[2]


def auth_remaining_ms(authorization: str) -> Optional[int]:
    """Token expiry is usually in the 4th | segment as unix ms."""
    parsed = parse_auth_parts(authorization)
    if not parsed:
        return None
    _, _, token = parsed
    strs = token.split("|")
    if len(strs) < 4:
        return None
    try:
        exp = int(strs[3])
    except ValueError:
        return None
    return exp - int(time.time() * 1000)


def refresh_token(authorization: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Try authTokenRefresh.do.
    Returns (new_auth_or_none, error_or_none).
    If remaining > 15 days, returns (same auth, None).
    """
    parsed = parse_auth_parts(authorization)
    if not parsed:
        return None, "authorization decode failed"
    prefix, account, token = parsed
    rem = auth_remaining_ms(authorization)
    if rem is not None and rem > 1000 * 60 * 60 * 24 * 15:
        return authorization if not authorization.lower().startswith("basic ") else authorization[6:].strip(), None
    if rem is not None and rem < 0:
        return None, "authorization has expired"

    url = "https://aas.caiyun.feixin.10086.cn:443/tellin/authTokenRefresh.do"
    body = f"<root><token>{token}</token><account>{account}</account><clienttype>656</clienttype></root>"
    try:
        with _http_client(follow_redirects=True) as client:
            r = client.post(
                url,
                content=body.encode("utf-8"),
                headers={"Content-Type": "application/xml", "Accept": "application/xml, text/xml, */*"},
            )
        text = r.text or ""
        # simple XML parse
        m_ret = re.search(r"<return>([^<]*)</return>", text, re.I)
        m_tok = re.search(r"<token>([^<]*)</token>", text, re.I)
        m_desc = re.search(r"<desc>([^<]*)</desc>", text, re.I)
        ret = (m_ret.group(1) if m_ret else "").strip()
        new_tok = (m_tok.group(1) if m_tok else "").strip()
        desc = (m_desc.group(1) if m_desc else "").strip()
        if ret != "0" or not new_tok:
            return None, f"refresh failed return={ret} desc={desc or text[:120]}"
        new_auth = base64.b64encode(f"{prefix}:{account}:{new_tok}".encode("utf-8")).decode("ascii")
        return new_auth, None
    except Exception as e:
        return None, f"refresh request failed: {e}"


def _extract_rmkey(mail_cookies: str) -> str:
    for part in (mail_cookies or "").split(";"):
        part = part.strip()
        if part.startswith("RMKEY="):
            return part
    return ""


def _merge_set_cookie(existing: str, set_cookie_headers) -> str:
    jar: Dict[str, str] = {}
    for part in (existing or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    # httpx may give list or single
    if isinstance(set_cookie_headers, str):
        headers = [set_cookie_headers]
    else:
        headers = list(set_cookie_headers or [])
    for sc in headers:
        # name=value; Path=...
        first = sc.split(";", 1)[0].strip()
        if "=" in first:
            k, v = first.split("=", 1)
            jar[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in jar.items())


def step1_password_login(username: str, password: str, mail_cookies: str) -> Tuple[str, str]:
    """Returns (sid, updated_mail_cookies)."""
    hashed = _sha1_hex(f"fetion.com.cn:{password}")
    cguid = str(int(time.time() * 1000))
    login_url = "https://mail.10086.cn/Login/Login.ashx"
    u_b64 = base64.b64encode(username.encode("utf-8")).decode("ascii")
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://mail.10086.cn",
        "referer": (
            f"https://mail.10086.cn/default.html?&s=1&v=0&u={u_b64}"
            f"&m=1&ec=S001&resource=indexLogin&clientid=1003&auto=on&cguid={cguid}&mtime=45"
        ),
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
        ),
        "Cookie": mail_cookies,
    }
    data = {
        "UserName": username,
        "passOld": "",
        "auto": "on",
        "Password": hashed,
        "webIndexPagePwdLogin": "1",
        "pwdType": "1",
        "clientId": "1003",
        "authType": "2",
    }
    with _http_client(follow_redirects=False) as client:
        r = client.post(login_url, data=data, headers=headers)
    location = r.headers.get("Location") or r.headers.get("location") or ""
    sid = ""
    extracted_cguid = ""
    m = re.search(r"sid=([^&]+)", location)
    if m:
        sid = m.group(1)
    m = re.search(r"cguid=([^&]+)", location)
    if m:
        extracted_cguid = m.group(1)

    # Set-Cookie may be multi
    set_cookies = []
    # httpx Headers.get_list if available
    try:
        set_cookies = r.headers.get_list("set-cookie")  # type: ignore[attr-defined]
    except Exception:
        sc = r.headers.get("set-cookie")
        if sc:
            set_cookies = [sc]

    if not sid or not extracted_cguid:
        for cookie_str in set_cookies:
            m = re.search(r"Os_SSo_Sid=([^;]+)", cookie_str)
            if m and not sid:
                sid = m.group(1)
            m = re.search(r"cguid=([^;]+)", cookie_str)
            if m and not extracted_cguid:
                extracted_cguid = m.group(1)

    if not sid:
        raise RuntimeError("邮箱登录失败：未拿到 sid（请检查账号密码与邮箱 Cookie，尤其 Os_SSo_Sid/RMKEY）")

    new_cookies = _merge_set_cookie(mail_cookies, set_cookies)
    return sid, new_cookies


def step2_get_artifact(sid: str, mail_cookies: str) -> str:
    cguid = str(int(time.time() * 1000))
    url = (
        "https://smsrebuild1.mail.10086.cn/setting/s"
        f"?func={quote('umc:getArtifact')}&sid={sid}&cguid={cguid}"
    )
    rmkey = _extract_rmkey(mail_cookies)
    if not rmkey:
        raise RuntimeError("MailCookies 中缺少 RMKEY，请从 mail.10086.cn 重新复制 Cookie")
    headers = {
        "Cookie": rmkey,
        "Content-Type": "text/xml; charset=utf-8",
        "Accept-Encoding": "gzip",
        "User-Agent": "okhttp/4.12.0",
    }
    with _http_client(follow_redirects=True) as client:
        r = client.post(url, headers=headers)
    try:
        j = r.json()
    except Exception:
        # sometimes body is not pure json
        text = r.text or ""
        m = re.search(r'"artifact"\s*:\s*"([^"]+)"', text)
        if m:
            return m.group(1)
        raise RuntimeError(f"getArtifact 响应无效: {text[:200]}")
    artifact = ""
    if isinstance(j, dict):
        var = j.get("var") or {}
        if isinstance(var, dict):
            artifact = var.get("artifact") or ""
        if not artifact:
            artifact = j.get("artifact") or ""
    if not artifact:
        raise RuntimeError("getArtifact 未返回 artifact")
    return str(artifact)


def _encrypted_request(url: str, body: dict, headers: dict, aes_key_hex: str) -> bytes:
    aes_key = bytes.fromhex(aes_key_hex)
    sorted_json = _sorted_json_stringify(body)
    iv = os.urandom(16)
    encrypted = _aes_cbc_encrypt(sorted_json.encode("utf-8"), aes_key, iv)
    payload = base64.b64encode(iv + encrypted).decode("ascii")
    with _http_client(follow_redirects=True) as client:
        r = client.post(url, content=payload.encode("utf-8"), headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"encrypted request HTTP {r.status_code}: {r.text[:200]}")
    resp_body = r.content or b""
    if resp_body[:1] == b"{":
        return resp_body
    decoded = base64.b64decode(resp_body)
    if len(decoded) < 16:
        raise RuntimeError("encrypted response too short")
    return _aes_cbc_decrypt(decoded[16:], aes_key, decoded[:16])


def step3_third_login(username: str, dycpwd: str) -> Tuple[str, str, str]:
    """Returns (authorization_b64_no_basic, account, user_domain_id)."""
    sso_url = "https://user-njs.yun.139.com/user/thirdlogin"
    body = {
        "clientkey_decrypt": CLIENTKEY_DECRYPT,
        "clienttype": "886",
        "cpid": "507",
        "dycpwd": dycpwd,
        "extInfo": {"ifOpenAccount": "0"},
        "loginMode": "0",
        "msisdn": username,
        "pintype": "13",
        "secinfo": _sha1_hex(f"fetion.com.cn:{dycpwd}").upper(),
        "version": "20250901",
    }
    headers = {
        "hcy-cool-flag": "1",
        "x-huawei-channelSrc": "10246600",
        "x-sdk-channelSrc": "",
        "x-MM-Source": "0",
        "x-UserAgent": "android|23116PN5BC|android15|1.2.6|||1440x3200|10246600",
        "x-DeviceInfo": "4|127.0.0.1|5|1.2.6|Xiaomi|23116PN5BC||02-00-00-00-00-00|android 15|1440x3200|android|||",
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept-Encoding": "gzip",
        "User-Agent": "okhttp/3.12.2",
    }
    layer1 = _encrypted_request(sso_url, body, headers, KEY_HEX_1)
    j1 = json.loads(layer1.decode("utf-8"))
    hex_inner = (j1.get("data") or "") if isinstance(j1, dict) else ""
    if not hex_inner:
        raise RuntimeError("thirdlogin 缺少 data 字段")
    key2 = bytes.fromhex(KEY_HEX_2)
    final_bytes = _aes_ecb_decrypt(bytes.fromhex(hex_inner), key2)
    final = json.loads(final_bytes.decode("utf-8"))
    auth_token = final.get("authToken") or ""
    account = final.get("account") or ""
    user_domain_id = final.get("userDomainId") or ""
    if not auth_token or not account:
        raise RuntimeError("thirdlogin 未返回 authToken/account")
    new_auth = base64.b64encode(f"pc:{account}:{auth_token}".encode("utf-8")).decode("ascii")
    return new_auth, account, str(user_domain_id or "")


def login_with_password(username: str, password: str, mail_cookies: str) -> Dict[str, str]:
    if not username or not password or not mail_cookies:
        raise RuntimeError("username / password / mail_cookies 不能为空")
    sid, new_cookies = step1_password_login(username, password, mail_cookies)
    logger.info("yun139 step1 login ok sid=%s…", sid[:8] if sid else "")
    artifact = step2_get_artifact(sid, new_cookies or mail_cookies)
    logger.info("yun139 step2 artifact ok")
    new_auth, account, udid = step3_third_login(username, artifact)
    logger.info("yun139 step3 thirdlogin ok account=%s", account)
    return {
        "authorization": new_auth,
        "mail_cookies": new_cookies or mail_cookies,
        "account": account,
        "user_domain_id": udid,
    }


def ensure_authorization(
    authorization: str = "",
    username: str = "",
    password: str = "",
    mail_cookies: str = "",
    *,
    force_login: bool = False,
) -> Dict[str, str]:
    """
    Ensure a usable Authorization.
    Returns dict with keys: authorization, mail_cookies?, account?, refreshed(bool), source
    """
    auth = (authorization or "").strip()
    if auth.lower().startswith("basic "):
        auth = auth[6:].strip()

    if force_login:
        if username and password and mail_cookies:
            out = login_with_password(username, password, mail_cookies)
            out["refreshed"] = "1"
            out["source"] = "password_login"
            return out
        raise RuntimeError("强制登录需要 username + password + mail_cookies")

    if auth:
        new_auth, err = refresh_token(auth)
        if new_auth:
            return {
                "authorization": new_auth,
                "refreshed": "1" if new_auth != auth else "0",
                "source": "refresh" if new_auth != auth else "keep",
            }
        logger.warning("yun139 refresh failed: %s — try password login", err)
        if username and password and mail_cookies:
            out = login_with_password(username, password, mail_cookies)
            out["refreshed"] = "1"
            out["source"] = "password_login_after_refresh_fail"
            return out
        # 无密码回退：保留原 Authorization，让业务请求自己报错（便于用户补 Cookie）
        rem = auth_remaining_ms(auth)
        if rem is not None and rem < 0:
            raise RuntimeError(
                f"Authorization 已过期且无邮箱 Cookie+密码回退。请后台填写 OpenList 长期绑定，或更新 Authorization。"
                f"（刷新失败: {err}）"
            )
        logger.warning("keep existing authorization after refresh fail: %s", err)
        return {
            "authorization": auth,
            "refreshed": "0",
            "source": "keep_after_refresh_fail",
            "refresh_error": err or "",
        }

    if username and password and mail_cookies:
        out = login_with_password(username, password, mail_cookies)
        out["refreshed"] = "1"
        out["source"] = "password_login"
        return out

    raise RuntimeError("未配置 Authorization，且未同时提供邮箱 Cookie + 用户名 + 密码")
