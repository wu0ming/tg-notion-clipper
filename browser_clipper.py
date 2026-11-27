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
from config import allowed_user_ids, env, resolve_user_context
from gemini_processor import AnalysisResult, analyze_content, match_category
from notion_writer import _clean_id, ensure_markdown_child, find_existing, save_to_notion
from utils import extract_project_key, normalize_url, truncate, url_fingerprint

MAX_CLIP_BYTES = 480 * 1024
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


def clip_token_user(authorization: str) -> int | None:
    scheme, _, token = authorization.partition(" ")
    key = _clip_signer()
    if scheme.lower() != "bearer" or not key or not (match := TOKEN_RE.fullmatch(token)):
        return None
    now = int(time.time())
    user_id, expires = map(int, match.groups())
    if not now < expires <= now + TOKEN_LIFETIME or user_id not in allowed_user_ids():
        return None
    payload, signature = token.rsplit(".", 1)
    expected = hmac.digest(key, payload.encode(), "sha256").hex()
    return user_id if hmac.compare_digest(signature, expected) else None


def authorized_clip(authorization: str) -> bool:
    return clip_token_user(authorization) is not None


def _markdown_document(clip: BrowserClip) -> str:
    heading = " ".join(clip.title.splitlines()).strip()
    document = [f"# {heading}", f"来源：<{clip.url}>", f"范围：{'整页' if clip.mode == 'page' else '选区'}"]
    if clip.annotation.strip():
        document += ["## 批注", clip.annotation.strip()]
    document += ["## 剪藏原文", clip.markdown]
    return "\n\n".join(document) + "\n"


async def archive_browser_clip(clip: BrowserClip, *, user_id: int | None = None) -> dict:
    ctx = resolve_user_context(user_id) if user_id is not None else None
    scope = ctx.notion_scope() if ctx is not None else None
    tenant_id = ctx.user_id if ctx is not None else 0

    raw = _markdown_document(clip)
    clip_key = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    has_inbox = bool(ctx.inbox_db) if ctx is not None else bool(env("NOTION_DB_INBOX"))
    find_kwargs = {"scope": scope} if scope is not None else {}
    for inbox in [False, True] if has_inbox else [False]:
        if existing := await find_existing(clip.url, inbox=inbox, clip_key=clip_key, **find_kwargs):
            await ensure_markdown_child(existing, raw, **find_kwargs)
            return {"ok": True, "url": existing, "duplicate": True}

    status = "PASS"
    analyze_kwargs = {}
    if ctx is not None:
        analyze_kwargs = {"tenant_id": tenant_id, "config_db": ctx.config_db, "notion_token": ctx.notion_token}
    try:
        result, _, _ = await analyze_content("浏览器剪藏", clip.title, clip.markdown, clip.annotation, status,
                                              input_type="Text", url=clip.url, **analyze_kwargs)
    except Exception as exc:
        log.warning("浏览器剪藏 AI 分类待处理：%s", type(exc).__name__)
        cat_kwargs = {"config_db": ctx.config_db, "notion_token": ctx.notion_token} if ctx is not None else {}
        categories, _ = await get_categories(**cat_kwargs)
        result = AnalysisResult(title=truncate(clip.title, 40), category=match_category(categories, "")["name"],
                                tags=["待整理"], summary="浏览器剪藏原文和批注已保存，待 AI 分类。", key_points=[])
        status = "AI_PENDING"

    scope_label = "整页" if clip.mode == "page" else "选区"
    proj_key = extract_project_key(clip.url, clip.markdown, "Web")
    save_kwargs: dict = {
        "inbox": status == "AI_PENDING",
        "input_type": "Text",
        "fetch_tier": "Browser-Markdown",
        "original_url": clip.url,
        "clip_key": clip_key,
    }
    if proj_key:
        save_kwargs["project_key"] = proj_key
    if scope is not None:
        save_kwargs["scope"] = scope
    url, duplicate = await save_to_notion(result, "Web", clip.url, status, "", clip.annotation,
                                         f"浏览器提交的 Markdown（{scope_label}）", **save_kwargs)
    if not duplicate:
        await ensure_markdown_child(url, raw, **find_kwargs)
    try:
        from clip_store import get_store, store_available
        if not store_available():
            return {"ok": True, "url": url, "duplicate": duplicate, "status": status}
        norm = normalize_url(clip.url)
        get_store().upsert_clip(
            page_id=_clean_id(url), page_url=url, tenant_id=tenant_id,
            canonical_url=norm, url_hash=url_fingerprint(norm), project_key=proj_key,
            clip_key=clip_key, title=result.title, category=result.category,
            entity_type=getattr(result, "entity_type", "通用"), tags=result.tags,
            source="Web", status=status, fetch_tier="Browser-Markdown",
            summary=result.summary, content_preview=clip.markdown[:8000], user_note=clip.annotation,
        )
    except Exception:
        pass
    return {"ok": True, "url": url, "duplicate": duplicate, "status": status}
