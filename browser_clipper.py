"""Receive Markdown captured in the browser and archive it through the existing pipeline."""
import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from category_config import get_categories
from config import allowed_user_ids, env
from gemini_processor import AnalysisResult, analyze_content, match_category
from notion_writer import find_existing, save_to_notion
from utils import truncate

MAX_CLIP_BYTES = 768 * 1024
TOKEN_LIFETIME = 180 * 24 * 60 * 60
TOKEN_RE = re.compile(r"v1\.([1-9][0-9]{0,18})\.([0-9]{10})\.[A-Za-z0-9_-]{16}\.[0-9a-f]{64}")
log = logging.getLogger("clipper")


class BrowserClip(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=250)
    url: str = Field(min_length=1, max_length=1900)
    mode: Literal["page", "selection"]
    markdown: str = Field(min_length=1, max_length=180000)
    annotation: str = Field(default="", max_length=8000)

    @field_validator("title", "markdown")
    @classmethod
    def check_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Empty clip")
        return value

    @field_validator("url")
    @classmethod
    def check_source(cls, value: str) -> str:
        if any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("Invalid source URL")
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Invalid source URL")
        return value


def _clip_signer() -> bytes | None:
    secret = env("BROWSER_CLIP_SECRET") or env("WEBHOOK_SECRET")
    return hmac.digest(secret.encode(), b"tg-notion-clipper:browser-only:v1", "sha256") if secret else None


def issue_clip_token(user_id: int) -> str | None:
    key = _clip_signer()
    if key is None or user_id not in allowed_user_ids() or user_id <= 0:
        return None
    payload = f"v1.{user_id}.{int(time.time()) + TOKEN_LIFETIME}.{secrets.token_urlsafe(12)}"
    return payload + "." + hmac.digest(key, payload.encode(), "sha256").hex()


def authorized_clip(authorization: str) -> bool:
    scheme, _, token = authorization.partition(" ")
    key = _clip_signer()
    if scheme.lower() != "bearer" or not key or not (match := TOKEN_RE.fullmatch(token)):
        return False
    now = int(time.time())
    user_id, expires = map(int, match.groups())
    if not now < expires <= now + TOKEN_LIFETIME or user_id not in allowed_user_ids():
        return False
    payload, signature = token.rsplit(".", 1)
    expected = hmac.digest(key, payload.encode(), "sha256").hex()
    return hmac.compare_digest(signature, expected)


def _markdown_file(clip: BrowserClip) -> bytes:
    heading = " ".join(clip.title.splitlines()).strip()
    document = [f"# {heading}", f"来源：<{clip.url}>", f"范围：{'整页' if clip.mode == 'page' else '选区'}"]
    if clip.annotation.strip():
        document += ["## 批注", clip.annotation.strip()]
    document += ["## 剪藏原文", clip.markdown]
    return ("\n\n".join(document) + "\n").encode("utf-8")


async def archive_browser_clip(clip: BrowserClip) -> dict:
    raw = _markdown_file(clip)
    clip_key = hashlib.sha256(raw).hexdigest()
    for inbox in [False, True] if env("NOTION_DB_INBOX") else [False]:
        if existing := await find_existing(clip.url, inbox=inbox, clip_key=clip_key):
            return {"ok": True, "url": existing, "duplicate": True}

    status = "PASS"
    try:
        result, _, _ = await analyze_content("浏览器剪藏", clip.title, clip.markdown, clip.annotation, status,
                                              input_type="Text")
    except Exception as exc:
        log.warning("浏览器剪藏 AI 分类待处理：%s", type(exc).__name__)
        categories, _ = await get_categories()
        result = AnalysisResult(title=truncate(clip.title, 40), category=match_category(categories, "")["name"],
                                tags=["待整理"], summary="浏览器剪藏原文和批注已保存，待 AI 分类。", key_points=[])
        status = "AI_PENDING"

    title = re.sub(r'[^\w .-]', "_", clip.title, flags=re.UNICODE)[:80].strip(" ._")
    file_name = f"{title or 'browser-clip'}.md"
    scope = "整页" if clip.mode == "page" else "选区"
    # Use the saved Markdown as the text source. The archive never re-fetches the browser's URL.
    url, duplicate = await save_to_notion(result, "Web", clip.url, status, clip.markdown, clip.annotation,
                                         f"浏览器提交的 Markdown（{scope}）", inbox=status == "AI_PENDING",
                                         input_type="Text", fetch_tier="Browser-Markdown", original_url=clip.url,
                                         attachment=(raw, "text/markdown", file_name), clip_key=clip_key)
    return {"ok": True, "url": url, "duplicate": duplicate, "status": status}
