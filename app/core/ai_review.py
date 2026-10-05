"""AI 内容安全审核:用普通 LLM(OpenAI 兼容接口)实现。

分层:
  - 文件名/标题审核:init 时全量走(零流量)
  - 小文件内容审核:文本类提文本、图片类转 base64 多模态,前端在 complete 前
    主动 POST 一份给 /api/ai-review/file
原则:宽松判断(仅明显违规才拒),AI 调用失败/未启用一律放行(skip),
审核系统故障不能拖垮上传主服务。reject 不自动拉黑 SHA(防误判锁死),
拉黑走举报处理/手动。
"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Dict

import httpx

from app.config_store import load_config

logger = logging.getLogger("fts.ai_review")

TEXT_EXTS = {"txt", "md", "csv", "log", "json", "html", "htm", "xml", "srt", "yml", "yaml", "ini"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
REVIEW_TEXT_CHARS = 6000
IMAGE_MAX_BYTES = 8 * 1024 * 1024  # base64 膨胀 4/3,过大 LLM 会拒收

GUARD_SYSTEM = (
    "你是文件分享平台的内容安全审核员。判断给定内容是否明显违规:"
    "涉黄涉政涉暴恐、违法交易(毒品/枪支/证件/个人信息)、赌博诈骗、"
    "病毒木马恶意软件、教唆犯罪、侵权盗版牟利等。"
    "宽松原则:仅当内容明显违规时才拒绝,拿不准一律通过。"
    '只输出 JSON,不要输出其他任何文字:{"verdict": "pass" 或 "reject", "reason": "简要中文理由"}'
)


def cfg() -> Dict[str, Any]:
    return load_config().get("ai_review") or {}


def enabled() -> bool:
    c = cfg()
    return bool(
        c.get("enabled")
        and (c.get("api_key") or "").strip()
        and (c.get("base_url") or "").strip()
        and (c.get("model") or "").strip()
    )


def max_review_mb() -> int:
    try:
        v = int(cfg().get("max_review_mb") or 10)
    except (TypeError, ValueError):
        v = 10
    return max(1, min(v, 100))


def _chat(messages, *, model: str = "", timeout: float = 0) -> str:
    c = cfg()
    base = (c.get("base_url") or "").strip().rstrip("/")
    key = (c.get("api_key") or "").strip()
    m = model or (c.get("model") or "").strip()
    t = float(timeout or c.get("timeout") or 30)
    if not base or not key or not m:
        raise RuntimeError("AI 审核配置不完整")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body = {"model": m, "messages": messages, "temperature": 0}
    with httpx.Client(timeout=httpx.Timeout(t, connect=10.0)) as client:
        r = client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
    if r.status_code != 200:
        raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:150]}")
    j = r.json()
    return ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def _parse_verdict(content: str) -> Dict[str, str]:
    m = re.search(r"\{[^{}]*\}", content or "", re.S)
    if not m:
        raise ValueError(f"LLM 输出无法解析: {(content or '')[:80]}")
    j = json.loads(m.group(0))
    verdict = str(j.get("verdict") or "").strip().lower()
    reason = str(j.get("reason") or "").strip()[:200]
    if verdict in ("reject", "blocked", "violation", "违规"):
        return {"verdict": "reject", "reason": reason or "内容未通过安全审核"}
    return {"verdict": "pass", "reason": reason}


def review_filename(filename: str, title: str = "") -> Dict[str, str]:
    """文件名/标题审核:init 时全量走。失败放行(skip)。"""
    if not enabled():
        return {"verdict": "skip", "reason": "AI 审核未启用"}
    text = f"文件名:{filename}"
    if title:
        text += f"\n分享标题:{title}"
    try:
        content = _chat(
            [{"role": "system", "content": GUARD_SYSTEM}, {"role": "user", "content": text}]
        )
        return _parse_verdict(content)
    except Exception as e:
        logger.warning("ai filename review failed (%s): %s", filename, e)
        return {"verdict": "skip", "reason": f"AI 调用失败,已放行: {e}"}


def _review_text(filename: str, text: str) -> Dict[str, str]:
    text = text[:REVIEW_TEXT_CHARS]
    user = f"文件名:{filename}\n\n以下为文件文本内容(可能截断):\n{text}"
    content = _chat(
        [{"role": "system", "content": GUARD_SYSTEM}, {"role": "user", "content": user}]
    )
    return _parse_verdict(content)


def _review_image(filename: str, data: bytes, mime: str) -> Dict[str, str]:
    vision = (cfg().get("vision_model") or "").strip() or (cfg().get("model") or "").strip()
    b64 = base64.b64encode(data).decode()
    messages = [
        {"role": "system", "content": GUARD_SYSTEM},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"文件名:{filename}\n请审核这张图片内容是否违规。"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        },
    ]
    content = _chat(messages, model=vision, timeout=max(60.0, float(cfg().get("timeout") or 30)))
    return _parse_verdict(content)


def review_content(filename: str, data: bytes, mime: str = "") -> Dict[str, str]:
    """小文件内容审核入口:按扩展名分流。不支持/超限 → skip 放行。"""
    if not enabled():
        return {"verdict": "skip", "reason": "AI 审核未启用"}
    name = filename or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if len(data) > IMAGE_MAX_BYTES:
        return {"verdict": "skip", "reason": "文件过大,跳过内容审核(已做文件名审核)"}
    try:
        if ext in IMAGE_EXTS:
            return _review_image(name, data, mime or "image/jpeg")
        if ext in TEXT_EXTS:
            return _review_text(name, data.decode("utf-8", "replace"))
        return {"verdict": "skip", "reason": "该类型暂不支持内容审核(已做文件名审核)"}
    except Exception as e:
        logger.warning("ai content review failed (%s): %s", filename, e)
        return {"verdict": "skip", "reason": f"AI 调用失败,已放行: {e}"}


def test_connection() -> Dict[str, Any]:
    """后台测试按钮:发一条最小请求验证配置。"""
    if not enabled():
        return {"ok": False, "error": "AI 审核未启用或配置不完整(base_url/api_key/model)"}
    try:
        content = _chat(
            [
                {"role": "system", "content": GUARD_SYSTEM},
                {"role": "user", "content": '测试连通性。只输出 JSON: {"verdict": "pass", "reason": "ok"}'},
            ],
            timeout=20,
        )
        parsed = _parse_verdict(content)
        return {"ok": True, "reply": parsed}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
