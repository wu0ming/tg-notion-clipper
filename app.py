"""
Description: Telegram Bot Webhook 入口（部署于 Google Cloud Run）+ 本地轮询共用的消息处理流水线
Changelog:
- v1.0–v2.3: 同步请求处理、update_id 去重、Notion 动态分类、URL 预查重、转发来源隔离、GitHub 结构化抓取、Flash-Lite 成本控制
- v2.4–v3.0 (2026-11):
  - #5 Telegram 图文相册聚合与轻量知识单元拆分、跨来源 ProjectKey 增量合并
  - #2/#4 共享视频文件与视频链接按需拆解/关键帧流水线
  - #3 多用户独立 Notion 知识库上下文隔离
  - #11 六类实体感知模板与 PDF 文本层优先
  - #12 Telegram Inline Keyboard 交互纠偏、/find /recent /stats 命令与 FTS5 索引同步
  - #13 统一 /ingest 入口、持久任务队列与租户月度额度控制
"""
import asyncio
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from album_aggregator import (
    combine_unit_materials,
    coordinator as album_coordinator,
    parse_album_member,
    plan_knowledge_units,
    strip_control_hashtags,
)
from browser_clipper import BrowserClip, MAX_CLIP_BYTES, archive_browser_clip, authorized_clip, clip_token_user, issue_clip_token
from category_config import get_categories
from clip_store import get_store, store_available
from config import UserContext, allowed_user_ids, env, get_user_configs, resolve_user_context
from gemini_processor import AnalysisResult, analyze_content, match_category, plan_album_units_llm, recover_web
from notion_writer import (
    _clean_id,
    append_telegram_note,
    close_client,
    enrich_notion_page,
    fetch_page_content,
    find_existing,
    find_existing_page,
    save_to_notion,
    update_page_tags,
    upload_file,
    _get_client as _notion_client,
)
from router import smart_fetch
from telegram_media import TelegramMedia, download_media, extract_media, extract_pdf_text_layer
from telegram_source import explicit_note, forward_source
from utils import (
    URL_RE,
    build_video_step_blocks,
    clean_tags,
    extract_project_key,
    extract_url,
    normalize_url,
    truncate,
    url_fingerprint,
)
from video_pipeline import analyze_instruction_video, classify_video_intent, extract_frames_from_bytes


class _CloudRunFormatter(logging.Formatter):
    """Cloud Run 环境下输出单行 JSON，便于 Cloud Logging 按 severity 和字段检索"""
    def format(self, record: logging.LogRecord) -> str:
        payload = {"severity": record.levelname, "message": record.getMessage(), "logger": record.name}
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging() -> None:
    handler = logging.StreamHandler()
    if os.environ.get("K_SERVICE"):
        handler.setFormatter(_CloudRunFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    # httpx 默认把完整请求 URL 写进 INFO 日志，Telegram 的 URL 里带 Bot Token，会泄露到 Cloud Logging
    for _name in ("httpx", "httpcore"):
        logging.getLogger(_name).setLevel(logging.WARNING)


_setup_logging()
log = logging.getLogger("clipper")

_tg_client: httpx.AsyncClient | None = None
_tg_loop: asyncio.AbstractEventLoop | None = None


def _get_tg_client() -> httpx.AsyncClient:
    """获取当前事件循环对应的 Telegram httpx 连接池"""
    global _tg_client, _tg_loop
    loop = asyncio.get_running_loop()
    if _tg_client is None or _tg_client.is_closed or _tg_loop is not loop:
        _tg_client = httpx.AsyncClient()
        _tg_loop = loop
    return _tg_client


async def _close_tg_client() -> None:
    global _tg_client, _tg_loop
    if _tg_client and not _tg_client.is_closed:
        await _tg_client.aclose()
    _tg_client = None
    _tg_loop = None


@asynccontextmanager
async def lifespan(app_instance):
    yield
    await _close_tg_client()
    await close_client()


app = FastAPI(title="tg-notion-clipper", lifespan=lifespan)

_seen_updates: "OrderedDict[int, None]" = OrderedDict()
_inflight_updates: set[int] = set()
_lock = asyncio.Lock()
_archive_slots: asyncio.Semaphore | None = None
_archive_loop: asyncio.AbstractEventLoop | None = None
_url_locks: WeakValueDictionary[tuple[asyncio.AbstractEventLoop, str], asyncio.Lock] = WeakValueDictionary()
_active_user_ctx: contextvars.ContextVar[UserContext | None] = contextvars.ContextVar("active_user_ctx", default=None)

HELP_TEXT = (
    "把链接、文字、图片、相册、语音、视频或 PDF 发给我，我会分类打标并归档到 Notion。\n"
    "· 链接或附件可附上「批注：内容」和 #标签；多图相册支持 #合并 / #拆分 指令。\n"
    "· 回复自己已归档的原消息并以「批注：」开头，可补充个人批注。\n"
    "· 👀 处理中 · 👌 已归档 · 👍 已收录 · 🤔 降级留存 · 👎 写入失败\n"
    "· /find 关键词 · /recent [数量] · /stats 统计用量\n"
    "· /categories 查看当前分类；/reload 立即重新读取 Notion「分类配置」表\n"
    "· /clipkey 获取浏览器剪藏配对码；/id 查看你的 Telegram 数字 ID"
)


def _store_enabled() -> bool:
    return store_available() and (bool(env("CLIPPER_DB_PATH")) or env("NOTION_TOKEN") not in {"", "x"})


def _maybe_store():
    return get_store() if _store_enabled() else None


def _ctx_scope(ctx: UserContext | None) -> dict | None:
    return ctx.notion_scope() if ctx is not None else None


async def tg_call(method: str, payload: dict) -> dict | None:
    token = env("TELEGRAM_BOT_TOKEN")
    try:
        client = _get_tg_client()
        response = await client.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload,
            timeout=2.0 if method in {"setMessageReaction", "sendChatAction", "answerCallbackQuery"} else 5.0,
        )
        data = response.json()
        if response.is_success and data.get("ok"):
            return data
        log.warning("Telegram %s 调用失败：HTTP %s", method, response.status_code)
    except Exception as exc:
        log.warning("Telegram %s 调用失败：%s", method, type(exc).__name__)
    return None


async def reply(chat_id: int, text: str, reply_to: int | None = None, reply_markup: dict | None = None) -> int | None:
    payload: dict = {"chat_id": chat_id, "text": truncate(text, 4000), "disable_web_page_preview": True}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    response = await tg_call("sendMessage", payload)
    return (response.get("result") or {}).get("message_id") if response else None


async def set_reaction(chat_id: int, message_id: int | None, emoji: str) -> None:
    if message_id:
        await tg_call("setMessageReaction", {"chat_id": chat_id, "message_id": message_id,
                                              "reaction": [{"type": "emoji", "emoji": emoji}]})


def build_clip_keyboard(clip_id: str, *, is_duplicate: bool = False, has_url: bool = False) -> dict | None:
    if not clip_id:
        return None
    if is_duplicate:
        return {"inline_keyboard": [[
            {"text": "📝 追加批注", "callback_data": f"note:{clip_id}"},
            {"text": "🔄 强制重抓", "callback_data": f"refetch:{clip_id}"},
        ]]}
    row = [
        {"text": "🔍 精读升级", "callback_data": f"deep:{clip_id}"},
        {"text": "🏷️ 改标签", "callback_data": f"tag:{clip_id}"},
        {"text": "📝 追加批注", "callback_data": f"note:{clip_id}"},
    ]
    if has_url:
        row.append({"text": "🔄 重抓", "callback_data": f"refetch:{clip_id}"})
    return {"inline_keyboard": [row[:3], row[3:]] if len(row) > 3 else [row]}


def _slots() -> asyncio.Semaphore:
    global _archive_slots, _archive_loop
    loop = asyncio.get_running_loop()
    if _archive_loop is not loop:
        _archive_slots = asyncio.Semaphore(3)
        _archive_loop = loop
    return _archive_slots


async def _claim_update(update_id: int) -> bool:
    async with _lock:
        if update_id in _seen_updates or update_id in _inflight_updates:
            return False
        _inflight_updates.add(update_id)
        return True


async def _finish_update(update_id: int, completed: bool) -> None:
    async with _lock:
        _inflight_updates.discard(update_id)
        if completed:
            _seen_updates[update_id] = None
            while len(_seen_updates) > 500:
                _seen_updates.popitem(last=False)


async def _find_duplicate(url: str, *, scope: dict | None = None, project_key: str = "") -> str | None:
    if not url and not project_key:
        return None
    if store := _maybe_store():
        tenant_id = (scope or {}).get("tenant_id", 0)
        if hit := store.find_match(tenant_id=tenant_id, canonical_url=url, project_key=project_key):
            return hit["page_url"]
    try:
        find_kwargs = {"scope": scope} if scope is not None else {}
        if project_key:
            find_kwargs["project_key"] = project_key
        existing = await find_existing(url, **find_kwargs)
        has_inbox = bool(scope.get("inbox_db")) if scope is not None else bool(env("NOTION_DB_INBOX"))
        if existing or not has_inbox:
            return existing
        return await find_existing(url, inbox=True, **find_kwargs)
    except Exception as exc:
        log.warning("Notion 预查重失败：%s", type(exc).__name__)
        return None


def _duplicate_receipt(url: str) -> tuple[str, str]:
    return f"♻️ 该链接已归档过，跳过重复写入：\n{url}", "👍"


def _budget_status(ctx: UserContext | None) -> tuple[bool, str]:
    """检查租户月度预算（80% 提醒，100% 按策略降级或停止付费）。"""
    if ctx is None or ctx.monthly_budget_usd <= 0:
        return False, ""
    store = _maybe_store()
    if not store:
        return False, ""
    spent = store.monthly_spend(ctx.user_id)
    ratio = spent / ctx.monthly_budget_usd
    if ratio >= 1.0:
        return True, f"本月额度已达上限（${spent:.4f} / ${ctx.monthly_budget_usd:.2f}）"
    if ratio >= 0.8:
        return False, f"额度提示：本月已用 {ratio * 100:.0f}%（${spent:.4f} / ${ctx.monthly_budget_usd:.2f}）"
    return False, ""


def _record_clip_index(ctx: UserContext | None, *, page_url: str, canonical_url: str, project_key: str,
                       telegram_key: str, album_key: str, result: AnalysisResult, source: str,
                       status: str, fetch_tier: str, content: str, user_note: str) -> str:
    store = _maybe_store()
    if not store or not page_url:
        return ""
    tenant_id = ctx.user_id if ctx is not None else 0
    try:
        return store.upsert_clip(
            page_id=_clean_id(page_url),
            page_url=page_url,
            tenant_id=tenant_id,
            canonical_url=canonical_url,
            url_hash=url_fingerprint(canonical_url) if canonical_url else "",
            project_key=project_key,
            telegram_key=telegram_key,
            album_key=album_key,
            title=result.title,
            category=result.category,
            entity_type=getattr(result, "entity_type", "通用"),
            tags=result.tags,
            source=source,
            status=status,
            fetch_tier=fetch_tier,
            summary=result.summary,
            content_preview=content[:10000],
            user_note=user_note,
        )
    except Exception:
        return ""


async def _find_cross_source_page(url: str, project_key: str, scope: dict | None) -> dict | None:
    if not project_key:
        return None
    if store := _maybe_store():
        tenant_id = (scope or {}).get("tenant_id", 0)
        if hit := store.find_match(tenant_id=tenant_id, project_key=project_key):
            return {"id": hit["page_id"], "url": hit["page_url"]}
    db = (scope or {}).get("archive_db") if scope is not None else env("NOTION_DB_ARCHIVE")
    if not db:
        return None
    try:
        return await find_existing_page(project_key=project_key, scope=scope)
    except Exception as exc:
        log.warning("跨来源匹配查询失败：%s", type(exc).__name__)
        return None


async def _archive_message(text: str, entities: list | None, message: dict,
                           media: TelegramMedia | None = None, *,
                           user_ctx: UserContext | None = None,
                           force_refetch: bool = False) -> tuple[str, str]:
    user_ctx = user_ctx if user_ctx is not None else _active_user_ctx.get()
    forward = forward_source(message)
    raw_url, accompanying = extract_url(text, entities)
    if not raw_url or media:
        return await _archive_clip(text, message, media, forward, raw_url, accompanying,
                                   user_ctx=user_ctx, force_refetch=force_refetch)

    try:
        url = normalize_url(raw_url)
    except ValueError:
        url = ""
    has_new_material = bool(message.get("message_id") and (forward or accompanying))
    is_video_instruction = classify_video_intent(accompanying) == "visual_steps" if url else False
    dedup = not has_new_material and not force_refetch and not is_video_instruction
    if not url or not dedup:
        return await _archive_clip(text, message, media, forward, raw_url, accompanying,
                                   url=url, deduplicate_by_url=dedup,
                                   user_ctx=user_ctx, force_refetch=force_refetch)
    tenant_tag = user_ctx.user_id if user_ctx is not None else 0
    key = (asyncio.get_running_loop(), f"{tenant_tag}:{url}")
    lock = _url_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _archive_clip(text, message, media, forward, raw_url, accompanying, url=url,
                                   user_ctx=user_ctx, force_refetch=force_refetch)


async def _archive_clip(text: str, message: dict, media: TelegramMedia | None, forward: dict,
                        raw_url: str | None, accompanying: str, *, url: str = "",
                        deduplicate_by_url: bool = True,
                        user_ctx: UserContext | None = None,
                        force_refetch: bool = False) -> tuple[str, str]:
    scope = _ctx_scope(user_ctx)
    tenant_id = user_ctx.user_id if user_ctx is not None else 0
    cat_kwargs = {"config_db": user_ctx.config_db, "notion_token": user_ctx.notion_token} if user_ctx is not None else {}

    user_note = accompanying_text = ""
    if not forward and (media or raw_url):
        user_note = text.strip() if media else accompanying
        if (note_text := explicit_note(user_note)) is not None:
            user_note = note_text
        else:
            accompanying_text = user_note
    tag_text = "" if forward else (user_note if media or raw_url else text)
    user_tags = clean_tags(re.findall(r"(?<!\w)#([\w]+)", URL_RE.sub(" ", tag_text)), limit=5)

    title = channel = note = model = ""
    content, source = ("", "Telegram") if forward else (text, "Note")
    input_type, status, tier = "Text", "PASS", ""
    media_bytes = None
    attachment = None
    attachments_list: list[tuple[bytes, str, str]] = []
    extra_blocks: list[dict] = []
    analyze = True
    fetched_model_content = None
    reference_links = ()
    project_key = extract_project_key(url or raw_url or "", text, source)
    explicit_split = bool(re.search(r"#独立\b|单独保存", tag_text))

    over_budget, budget_msg = _budget_status(user_ctx)

    if media:
        source, title, input_type, tier = "Telegram", media.file_name, media.input_type, "Direct-Multimodal"
        content = ""
        try:
            media_bytes = await download_media(_get_tg_client(), env("TELEGRAM_BOT_TOKEN"), media)
            attachment = (media_bytes, media.mime_type, media.file_name)
            attachments_list.append(attachment)
        except ValueError as exc:
            note = f"附件未下载：{exc}"
            status, tier, analyze = "FALLBACK", "Tier4-Fallback", False
        except Exception as exc:
            note = f"附件下载失败（{type(exc).__name__}）"
            status, tier, analyze = "FALLBACK", "Tier4-Fallback", False
    elif raw_url:
        input_type = "Link"
        if deduplicate_by_url:
            find_kwargs = {"scope": scope} if scope is not None else {}
            if duplicate := await _find_duplicate(url, **find_kwargs):
                return _duplicate_receipt(duplicate)

        # #4 按需视频图文步骤流（仅在明确请求拆解/步骤/截图时启动，普通视频链接走低成本字幕摘要）
        video_step_result = None
        if url and classify_video_intent(user_note or accompanying_text) == "visual_steps" and any(
            h in url for h in ("youtube.com", "youtu.be", "bilibili.com", "b23.tv")
        ):
            categories, _ = await get_categories(**cat_kwargs)
            async with httpx.AsyncClient(follow_redirects=True) as vclient:
                video_step_result = await analyze_instruction_video(vclient, url, user_note or accompanying_text, categories)

        if video_step_result:
            asset = video_step_result["asset"]
            result = video_step_result["result"]
            status = video_step_result["status"]
            note = video_step_result["note"]
            source = asset.source_type
            title = asset.title or url
            content = asset.render_archive_text()
            channel, tier = f"{source} 图文拆解", "Video-Pipeline"
            analyze = False
            upload_ids: dict[int, str] = {}
            for idx, (sec, f_bytes, f_mime) in enumerate(video_step_result["frames"]):
                try:
                    token_arg = scope.get("notion_token", "") if scope else ""
                    uid = await upload_file(_notion_client(), (f_bytes, f_mime, f"frame-{sec}s.jpg"), token=token_arg)
                    upload_ids[idx] = uid
                except Exception as exc:
                    note = "；".join(filter(None, (note, f"关键帧 {sec}s 上传失败（{type(exc).__name__}）")))
            extra_blocks = build_video_step_blocks(video_step_result["steps"], upload_ids)
        else:
            try:
                fetched = await smart_fetch(raw_url)
            except Exception as exc:
                source, content, status, note = "Web", "", "FALLBACK", f"链接抓取失败（{type(exc).__name__}）"
            else:
                source, title, content, status, note = (fetched.source_type, fetched.title, fetched.content,
                                                       fetched.status, fetched.note)
                channel, tier = fetched.channel, fetched.fetch_tier
                fetched_model_content = getattr(fetched, "model_content", None)
                reference_links = getattr(fetched, "reference_links", ())
                if fetched.clean_url != url:
                    url = fetched.clean_url
                    if deduplicate_by_url:
                        find_kwargs = {"scope": scope} if scope is not None else {}
                        if duplicate := await _find_duplicate(url, **find_kwargs):
                            return _duplicate_receipt(duplicate)
                project_key = extract_project_key(url, f"{text}\n{content}", source) or project_key
            if status == "NEEDS_GEMINI_WEB":
                try:
                    web_title, web_content, status, web_note = await recover_web(url, user_note or accompanying_text)
                except Exception as exc:
                    web_title, web_content, status, web_note = "", "", "FALLBACK", f"Gemini 联网失败（{type(exc).__name__}）"
                note = "；".join(filter(None, (note, web_note)))
                if status in {"PASS", "PARTIAL"} and web_content:
                    title, content, channel, tier = web_title or title, web_content, "Gemini 联网", "Tier3-GeminiWeb"
                else:
                    status = "FALLBACK"
            if status == "FALLBACK":
                tier, analyze = "Tier4-Fallback", False

    # #10 PDF 优先文本层；#2 视频超大时按需抽帧降级
    model_media_parts: list[tuple[bytes, str]] | None = None
    model_media_bytes = media_bytes
    model_media_mime = media.mime_type if media else None
    if input_type == "PDF" and media_bytes:
        if pdf_text := extract_pdf_text_layer(media_bytes):
            content = pdf_text
            model_media_bytes = None
            model_media_mime = None
            note = "；".join(filter(None, (note, "PDF 优先提取文本层")))
    elif input_type == "Video" and media_bytes:
        inline_limit = int(env("VIDEO_INLINE_MAX_BYTES") or 18 * 1024 * 1024)
        if len(media_bytes) > inline_limit or classify_video_intent(user_note or accompanying_text) == "visual_steps":
            frames = extract_frames_from_bytes(media_bytes, os.path.splitext(media.file_name)[1], media.duration)
            if frames:
                model_media_parts = [(b, m) for _, b, m in frames]
                model_media_bytes = None
                model_media_mime = None
                note = "；".join(filter(None, (note, f"已抽取 {len(frames)} 帧关键画面分析")))
            elif len(media_bytes) > inline_limit:
                note = "；".join(filter(None, (note, "视频体积超出内联分析上限且未安装 ffmpeg，已留存原附件")))
                status, tier, analyze = "FALLBACK", "Tier4-Fallback", False

    if analyze and over_budget and (user_ctx and user_ctx.quota_policy == "stop"):
        note = "；".join(filter(None, (note, budget_msg)))
        status, tier, analyze = "AI_PENDING", "Tier4-Fallback", False

    if analyze:
        model_content = content if fetched_model_content is None else fetched_model_content
        if forward and text:
            model_content = "\n\n".join(filter(None, (f"【Telegram 转发内容】\n{text}", model_content)))
        is_public = bool(raw_url and not forward and not user_note and not accompanying_text and not media_bytes and not model_media_parts)
        req_key = f"tg:{tenant_id}:{message.get('message_id')}" if message.get("message_id") else ""
        try:
            a_kwargs: dict = {
                "media_bytes": model_media_bytes,
                "media_mime": model_media_mime,
                "input_type": input_type,
                "user_hashtags": user_tags,
            }
            if model_media_parts:
                a_kwargs["media_parts"] = model_media_parts
            if url:
                a_kwargs["url"] = url
            if is_public:
                a_kwargs["is_public_only"] = True
            if over_budget:
                a_kwargs["force_complex"] = False
            if user_ctx is not None:
                a_kwargs.update(tenant_id=tenant_id, request_key=req_key, **cat_kwargs)
            result, _, model = await analyze_content(source, title, model_content, user_note, status, **a_kwargs)
        except Exception as exc:
            log.warning("Gemini 分类暂不可用：%s", type(exc).__name__)
            note = "；".join(filter(None, (note, f"AI 分类暂不可用（{type(exc).__name__}）")))
            status, tier, analyze = "AI_PENDING", "Tier4-Fallback", False

    if not analyze and "result" not in locals():
        categories, _ = await get_categories(**cat_kwargs)
        fallback = match_category(categories, "")
        summary = "AI 分类暂不可用，已留存原始素材，待整理。" if status == "AI_PENDING" else "原始内容暂时无法读取，已留存可用素材。"
        result = AnalysisResult(
            title=truncate(title or text.strip() or url or "待整理归档", 40),
            category=fallback["name"],
            tags=clean_tags([*user_tags, "待整理"], limit=5),
            summary=summary,
            key_points=[],
        )

    # #5 跨批次、跨来源增量合并（例如先存 X 帖文，再存同一 GitHub 仓库时合并为一页）
    if not explicit_split and not force_refetch and project_key and (
        existing_page := await _find_cross_source_page(url, project_key, scope)
    ):
        source_key = f"tg-{message.get('chat', {}).get('id', 0)}-{message.get('message_id', 0)}" if message.get("message_id") else url_fingerprint(url)[:12]
        enrich_kwargs = {"scope": scope} if scope is not None else {}
        page_url, already_enriched = await enrich_notion_page(
            existing_page["url"],
            source_type=source,
            url=url or raw_url or "",
            raw_content=content,
            user_note=user_note,
            accompanying_text=accompanying_text,
            forwarded_text=text if forward else "",
            forward_metadata=forward,
            key_points=result.key_points,
            attachments=attachments_list,
            tags=result.tags,
            source_key=source_key,
            **enrich_kwargs,
        )
        if already_enriched:
            return _duplicate_receipt(page_url)
        _record_clip_index(
            user_ctx, page_url=page_url, canonical_url=url or existing_page["url"],
            project_key=project_key, telegram_key=f"{message.get('chat', {}).get('id')}:{message.get('message_id')}" if message.get("message_id") else "",
            album_key="", result=result, source=source, status=status, fetch_tier=tier,
            content=content, user_note=user_note or accompanying_text,
        )
        return (f"🔗 已按同一项目（{project_key}）增量合并至已有归档页：\n"
                f"新增来源：{source} · {status}\n标题：{result.title}\nNotion：{page_url}"), "👌"

    inbox = status in {"FALLBACK", "AI_PENDING"}
    media_metadata = media.metadata(message) if media else None
    if media_metadata is not None and not media_bytes:
        media_metadata["archive_note"] = "附件保留在 Telegram，未能下载原文件。"

    save_kwargs: dict = {
        "inbox": inbox,
        "input_type": input_type,
        "fetch_tier": tier,
        "original_url": raw_url or "",
        "media_metadata": media_metadata,
        "attachment": attachment,
        "telegram_message": {"chat_id": (message.get("chat") or {}).get("id"), "message_id": message.get("message_id")},
        "forwarded_text": text if forward else "",
        "forward_metadata": forward,
        "accompanying_text": accompanying_text,
        "deduplicate_by_url": deduplicate_by_url,
        "reference_links": reference_links,
    }
    if project_key:
        save_kwargs["project_key"] = project_key
    if extra_blocks:
        save_kwargs["extra_blocks"] = extra_blocks
    if scope is not None:
        save_kwargs["scope"] = scope

    page_url, is_dup = await save_to_notion(result, source, url, status, content, user_note, note, **save_kwargs)
    log.info("archived source=%s status=%s model=%s tier=%s channel=%s dup=%s", source, status, model, tier, channel, is_dup)

    if is_dup:
        return _duplicate_receipt(page_url)

    tg_key = f"{(message.get('chat') or {}).get('id')}:{message.get('message_id')}" if message.get("message_id") else ""
    _record_clip_index(
        user_ctx, page_url=page_url, canonical_url=url, project_key=project_key,
        telegram_key=tg_key, album_key="", result=result, source=source,
        status=status, fetch_tier=tier, content=content, user_note=user_note or accompanying_text,
    )

    degraded = tier in {"Tier3-GeminiWeb", "Tier4-Fallback"}
    icon = "🟠" if degraded or status == "PARTIAL" else "✅"
    tags = " ".join(f"#{tag.replace(' ', '_')}" for tag in result.tags)
    lines = [f"{icon} 已归档至【{result.category}】（{source} · {status}）",
             f"标题：{result.title}", f"标签：{tags}", f"摘要：{result.summary}"]
    if inbox and ((scope or {}).get("inbox_db") if scope is not None else env("NOTION_DB_INBOX")):
        lines.append("去向：Notion 收件箱")
    if note:
        lines.append(f"提示：{truncate(note, 700)}")
    if budget_msg and not over_budget:
        lines.append(budget_msg)
    if channel:
        lines.append(f"渠道：{channel}")
    if tier:
        lines.append(f"抓取层级：{tier}")
    lines.append(f"Notion：{page_url}")
    return "\n".join(lines), "🤔" if degraded else "👌"


async def _archive_album(members: list[dict], *, user_ctx: UserContext | None = None) -> tuple[str, str]:
    """#5 相册聚合归档：下载一次素材、智能分组、组内缓存 URL、统一汇总回执。"""
    scope = _ctx_scope(user_ctx)
    tenant_id = user_ctx.user_id if user_ctx is not None else 0
    cat_kwargs = {"config_db": user_ctx.config_db, "notion_token": user_ctx.notion_token} if user_ctx is not None else {}

    parsed_members = [parse_album_member(m) for m in members]
    chat_id = (members[0].get("chat") or {}).get("id") or 0
    media_group_id = str(members[0].get("media_group_id") or "")
    album_key = album_coordinator.album_key(tenant_id, chat_id, media_group_id)

    downloaded: dict[int, tuple[bytes, str, str]] = {}
    download_errors: dict[int, str] = {}
    for m in parsed_members:
        if m.media:
            try:
                data = await download_media(_get_tg_client(), env("TELEGRAM_BOT_TOKEN"), m.media)
                downloaded[m.message_id] = (data, m.media.mime_type, m.media.file_name)
            except Exception as exc:
                download_errors[m.message_id] = f"{m.media.file_name} 下载失败（{type(exc).__name__}）"

    units = await plan_knowledge_units(parsed_members, downloaded, llm_planner=plan_album_units_llm)
    url_fetch_cache: dict[str, object] = {}
    summary_lines = [f"📦 相册已归档（共 {len(parsed_members)} 条素材 → {len(units)} 篇笔记）"]
    any_degraded = False
    store = _maybe_store()

    for unit in units:
        mats = combine_unit_materials(unit)
        raw_url = mats["raw_url"]
        user_note = mats["user_note"]
        accompanying_text = mats["accompanying_text"]
        forwarded_text = mats["forwarded_text"]
        forward_meta = mats["forward_metadata"]
        user_tags = mats["user_tags"]
        unit_attachments = [downloaded[m.message_id] for m in unit.members if m.message_id in downloaded]
        unit_media_meta = []
        unit_dl_notes = [download_errors[m.message_id] for m in unit.members if m.message_id in download_errors]
        for m in unit.members:
            if m.media:
                meta = m.media.metadata(m.message)
                if m.message_id not in downloaded:
                    meta["archive_note"] = "附件保留在 Telegram，未能下载原文件。"
                unit_media_meta.append(meta)

        url = ""
        title = channel = note = ""
        content = mats["plain_text"]
        source = "Telegram"
        status = "PASS"
        tier = "Direct-Multimodal" if unit_attachments else "Album-Aggregate"
        input_type = "Album" if len(unit.members) > 1 else (unit.members[0].media.input_type if unit.members[0].media else "Text")
        fetched_model_content = None
        reference_links = ()

        if raw_url:
            try:
                url = normalize_url(raw_url)
            except ValueError:
                url = ""
            if url:
                if url not in url_fetch_cache:
                    try:
                        url_fetch_cache[url] = await smart_fetch(raw_url)
                    except Exception as exc:
                        url_fetch_cache[url] = exc
                cached_fetch = url_fetch_cache[url]
                if isinstance(cached_fetch, Exception):
                    note = f"链接抓取失败（{type(cached_fetch).__name__}）"
                    status = "PARTIAL" if unit_attachments else "FALLBACK"
                else:
                    source = cached_fetch.source_type
                    title = cached_fetch.title
                    content = "\n\n".join(filter(None, (mats["plain_text"], cached_fetch.content)))
                    status = cached_fetch.status
                    note = cached_fetch.note
                    channel = cached_fetch.channel
                    fetched_model_content = getattr(cached_fetch, "model_content", None)
                    reference_links = getattr(cached_fetch, "reference_links", ())
                    if status == "NEEDS_GEMINI_WEB":
                        try:
                            wt, wc, ws, wn = await recover_web(url, user_note or accompanying_text)
                            note = "；".join(filter(None, (note, wn)))
                            if ws in {"PASS", "PARTIAL"} and wc:
                                title, content, status, channel = wt or title, wc, ws, "Gemini 联网"
                            else:
                                status = "PARTIAL" if unit_attachments else "FALLBACK"
                        except Exception as exc:
                            note = "；".join(filter(None, (note, f"Gemini 联网失败（{type(exc).__name__}）")))
                            status = "PARTIAL" if unit_attachments else "FALLBACK"

        if unit_dl_notes:
            note = "；".join(filter(None, (note, *unit_dl_notes)))
            if not unit_attachments and not content:
                status = "FALLBACK"

        model_media = [(b, mime) for b, mime, _ in unit_attachments if mime.startswith(("image/", "audio/", "video/")) or mime == "application/pdf"]
        model_content = content if fetched_model_content is None else "\n\n".join(filter(None, (mats["plain_text"], fetched_model_content)))
        if forwarded_text:
            model_content = "\n\n".join(filter(None, (f"【Telegram 转发内容】\n{forwarded_text}", model_content)))

        try:
            a_kwargs: dict = {
                "media_parts": model_media,
                "input_type": input_type,
                "user_hashtags": user_tags,
                "url": url,
            }
            if user_ctx is not None:
                a_kwargs.update(tenant_id=tenant_id, **cat_kwargs)
            result, _, _ = await analyze_content(source, title or unit.topic, model_content, user_note, status, **a_kwargs)
        except Exception as exc:
            log.warning("相册单元 AI 分类失败：%s", type(exc).__name__)
            categories, _ = await get_categories(**cat_kwargs)
            fallback = match_category(categories, "")
            status = "AI_PENDING"
            any_degraded = True
            note = "；".join(filter(None, (note, f"AI 分类暂不可用（{type(exc).__name__}）")))
            result = AnalysisResult(
                title=truncate(title or unit.topic or mats["plain_text"] or "相册待整理", 40),
                category=fallback["name"],
                tags=clean_tags([*user_tags, "待整理"], limit=5),
                summary="AI 分类暂不可用，已完整保留相册素材与附件。",
                key_points=[],
            )

        leader_mid = unit.members[0].message_id
        tg_msg = {"chat_id": chat_id, "message_id": leader_mid}
        if len(units) > 1:
            tg_msg["unit_index"] = unit.unit_index

        proj_key = extract_project_key(url or raw_url or "", content, source) if len(units) == 1 else ""
        save_kwargs: dict = {
            "inbox": status in {"FALLBACK", "AI_PENDING"},
            "input_type": input_type,
            "fetch_tier": tier,
            "original_url": raw_url or "",
            "media_metadata": unit_media_meta,
            "attachments": unit_attachments,
            "telegram_message": tg_msg,
            "forwarded_text": forwarded_text,
            "forward_metadata": forward_meta,
            "accompanying_text": accompanying_text,
            "deduplicate_by_url": False,
            "reference_links": reference_links,
            "album_key": album_key,
        }
        if proj_key:
            save_kwargs["project_key"] = proj_key
        if scope is not None:
            save_kwargs["scope"] = scope

        page_url, _ = await save_to_notion(result, source, url, status, content, user_note, note, **save_kwargs)
        tg_key_str = f"{chat_id}:{leader_mid}" + (f":u{unit.unit_index}" if len(units) > 1 else "")
        _record_clip_index(
            user_ctx, page_url=page_url, canonical_url=url, project_key=proj_key,
            telegram_key=tg_key_str, album_key=album_key, result=result, source=source,
            status=status, fetch_tier=tier, content=content, user_note=user_note or accompanying_text,
        )
        if store:
            store.save_album_unit(album_key, unit.unit_index, tenant_id, tg_key_str, page_url,
                                  [m.message_id for m in unit.members])

        if status in {"FALLBACK", "AI_PENDING", "PARTIAL"}:
            any_degraded = True
        summary_lines.append(f"• 【{result.category}】{result.title} ({len(unit.members)} 项) → {page_url}")
        if unit_dl_notes:
            summary_lines.append(f"  ⚠️ {'；'.join(unit_dl_notes)}")

    return "\n".join(summary_lines), "🤔" if any_degraded else "👌"


async def _append_late_album_member(album_key: str, message: dict, *, user_ctx: UserContext | None = None) -> tuple[str, str] | None:
    tenant_id = user_ctx.user_id if user_ctx is not None else 0
    scope = _ctx_scope(user_ctx)
    cat_kwargs = {"config_db": user_ctx.config_db, "notion_token": user_ctx.notion_token} if user_ctx is not None else {}
    store = _maybe_store()
    if not store:
        return None
    msg_id = int(message.get("message_id") or 0)
    if store.get_album_member_page(album_key, tenant_id, msg_id):
        return None
    units = store.get_album_units(album_key, tenant_id)
    if not units:
        return None
    target_url = units[0]["page_url"]
    member = parse_album_member(message)
    atts: list[tuple[bytes, str, str]] = []
    media_bytes: bytes | None = None
    if member.media:
        try:
            media_bytes = await download_media(_get_tg_client(), env("TELEGRAM_BOT_TOKEN"), member.media)
            atts.append((media_bytes, member.media.mime_type, member.media.file_name))
        except Exception as exc:
            log.warning("晚到相册媒体下载失败：%s", type(exc).__name__)

    late_summary = ""
    late_extracted = ""
    late_key_points: list[str] = []
    late_tags: list[str] = []
    user_note = member.explicit_user_note or ""
    accompanying = "" if member.explicit_user_note is not None else member.accompanying

    if media_bytes or member.text.strip():
        model_bytes = media_bytes
        model_mime = member.media.mime_type if member.media else None
        model_text = "" if member.forward else member.text
        if member.media and member.media.input_type == "PDF" and media_bytes:
            if pdf_text := extract_pdf_text_layer(media_bytes):
                model_text = pdf_text
                model_bytes = None
                model_mime = None
        try:
            a_kwargs: dict = {
                "media_bytes": model_bytes,
                "media_mime": model_mime,
                "input_type": member.media.input_type if member.media else "Text",
                "user_hashtags": [],
            }
            if user_ctx is not None:
                a_kwargs.update(tenant_id=tenant_id, request_key=f"tg-late:{tenant_id}:{msg_id}", **cat_kwargs)
            late_res, _, _ = await analyze_content(
                "Telegram",
                member.media.file_name if member.media else "相册补充素材",
                model_text if not member.forward else f"【Telegram 转发内容】\n{member.text}",
                user_note,
                "PASS",
                **a_kwargs,
            )
            late_summary = late_res.summary
            late_extracted = getattr(late_res, "extracted_text", "") or ""
            late_key_points = list(late_res.key_points or [])
            late_tags = list(late_res.tags or [])
        except Exception as exc:
            log.warning("晚到相册素材 AI 分析失败，仍保留原附件：%s", type(exc).__name__)

    combined_body = "\n\n".join(filter(None, (
        f"摘要：{late_summary}" if late_summary else "",
        f"视觉识别 / 文本提取：\n{late_extracted}" if late_extracted else "",
        ("" if member.forward else member.text) if not late_summary else "",
    )))
    enrich_kwargs = {"scope": scope} if scope is not None else {}
    await enrich_notion_page(
        target_url,
        source_type="Telegram",
        raw_content=combined_body,
        user_note=user_note,
        accompanying_text=accompanying,
        forwarded_text=member.text if member.forward else "",
        forward_metadata=member.forward,
        key_points=late_key_points,
        attachments=atts,
        tags=late_tags,
        source_key=f"album-late-{msg_id}",
        input_type_override="Album",
        **enrich_kwargs,
    )
    store.save_album_unit(album_key, units[0]["unit_index"], tenant_id, units[0]["telegram_key"],
                          target_url, [*units[0]["member_ids"], msg_id])
    extra_hint = f"\n补充识别：{truncate(late_summary, 240)}" if late_summary else ""
    return f"📎 晚到相册素材已完成识别并补充到原页面：{extra_hint}\n{target_url}", "👌"


async def archive_text(text: str, entities: list | None) -> str:
    receipt, _ = await _archive_message(text, entities, {})
    return receipt


async def _handle_note_reply(message: dict, target: dict, note: str, *, user_ctx: UserContext | None = None) -> bool:
    chat_id, message_id = message["chat"]["id"], message.get("message_id")
    target_id = target.get("message_id")
    if not note or not isinstance(message_id, int) or not isinstance(target_id, int) or (
        (target.get("chat") or {}).get("id") != chat_id or
        (target.get("from") or {}).get("id") != (message.get("from") or {}).get("id")
    ):
        await reply(chat_id, "请回复自己在当前聊天里已归档的原消息，并在「批注：」后填写内容。", message_id)
        return True
    try:
        if user_ctx is not None and get_user_configs():
            page_url = await append_telegram_note(chat_id, target_id, message_id, note, scope=_ctx_scope(user_ctx))
        else:
            page_url = await append_telegram_note(chat_id, target_id, message_id, note)
    except Exception as exc:
        log.warning("Telegram 批注写入失败：%s", type(exc).__name__)
        await reply(chat_id, "批注暂未写入，请稍后重试。", message_id)
        return False
    receipt = f"个人批注已保存\n{page_url}" if page_url else "找不到这条原消息对应的归档页面。"
    await reply(chat_id, receipt, message_id)
    return True


async def _handle_callback_query(cb: dict) -> bool:
    cb_id = str(cb.get("id") or "")
    user_id = (cb.get("from") or {}).get("id")
    ctx = resolve_user_context(user_id)
    if ctx is None:
        if cb_id:
            await tg_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "未授权操作", "show_alert": True})
        return True
    data = str(cb.get("data") or "")
    action, _, target_id = data.partition(":")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    store = _maybe_store()
    if store and cb_id and not store.claim_callback(cb_id, ctx.user_id, action, target_id):
        await tg_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "该操作已处理"})
        return True
    clip = store.get_clip(target_id, ctx.user_id) if store else None
    if not clip:
        await tg_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "未找到归档记录（可能尚未本地索引）"})
        return True

    scope = ctx.notion_scope()
    result_text = ""
    if action == "note":
        result_text = f"请回复原消息发送「批注：内容」，或打开页面补充：{clip['page_url']}"
        if chat_id:
            await reply(chat_id, result_text, msg.get("message_id"))
    elif action == "tag":
        result_text = f"修改标签请发送：/retag {clip['page_id']} 标签1 标签2"
        if chat_id:
            await reply(chat_id, result_text, msg.get("message_id"))
    elif action == "deep":
        try:
            page_data = await fetch_page_content(clip["page_id"], scope=scope)
            result, _, model = await analyze_content(
                clip["source"] or "Web", clip["title"], page_data["text"], "请精读深挖这篇文章并补充深度要点",
                "PASS", url=clip["canonical_url"], force_complex=True,
                tenant_id=ctx.user_id, config_db=ctx.config_db, notion_token=ctx.notion_token,
            )
            url, _ = await enrich_notion_page(
                clip["page_id"], source_type="深度精读", raw_content=result.summary,
                key_points=result.key_points, tags=result.tags, source_key=f"deep-{cb_id[:8]}", scope=scope,
            )
            result_text = f"🔍 已完成深度精读（{model}）：\n{url}"
        except Exception as exc:
            result_text = f"深度精读失败（{type(exc).__name__}）"
        if chat_id:
            await reply(chat_id, result_text, msg.get("message_id"))
    elif action == "refetch":
        if not clip.get("canonical_url"):
            result_text = "该记录没有原始链接，无法重抓。"
        else:
            receipt, _ = await _archive_message(clip["canonical_url"], [], {"chat": {"id": chat_id}, "message_id": msg.get("message_id")},
                                                user_ctx=ctx, force_refetch=True)
            result_text = receipt
        if chat_id:
            await reply(chat_id, result_text, msg.get("message_id"))

    if store and cb_id:
        store.finish_callback(cb_id, result_text)
    if cb_id:
        await tg_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": truncate(result_text or "已收到", 180)})
    return True


def _extract_receipt_notion_url(receipt: str) -> str | None:
    for line in reversed(receipt.splitlines()):
        if "https://www.notion.so/" in line or "https://notion.so/" in line or "https://app.notion.com/" in line:
            match = URL_RE.search(line)
            if match:
                return match.group(0)
    return None


async def _process_message(update: dict) -> bool:
    if cb := update.get("callback_query"):
        return await _handle_callback_query(cb)
    message = update.get("message") or update.get("edited_message")
    if not message:
        return True
    chat_id = message["chat"]["id"]
    message_id = message.get("message_id")
    user_id = (message.get("from") or {}).get("id")
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []

    forwarded = bool(forward_source(message))
    cmd = text.strip().split()[0].split("@")[0].lower() if not forwarded and text.strip().startswith("/") else ""
    if cmd == "/id":
        await reply(chat_id, f"你的 Telegram 数字 ID：{user_id}", message_id)
        return True
    user_ctx = resolve_user_context(user_id)
    if user_ctx is None:
        log.info("忽略非白名单或未配置知识库用户 %s", user_id)
        return True
    if not forwarded and message.get("text") and (target := message.get("reply_to_message")):
        if (note := explicit_note(text)) is not None:
            return await _handle_note_reply(message, target, note, user_ctx=user_ctx)
    if cmd in ("/start", "/help"):
        await reply(chat_id, HELP_TEXT, message_id)
        return True
    if cmd == "/clipkey":
        if message["chat"].get("type") != "private":
            await reply(chat_id, "请在与 Bot 的私聊中获取配对码。", message_id)
            return True
        token = issue_clip_token(user_id)
        message_text = f"浏览器剪藏配对码（180 天内有效）\n\n{token}\n\n将这行配对码粘贴到脚本的「配置配对码」。" if token else "浏览器剪藏配对暂不可用。"
        await reply(chat_id, message_text, message_id)
        return True
    if cmd in ("/reload", "/categories"):
        cats, src = await get_categories(force=(cmd == "/reload"), config_db=user_ctx.config_db,
                                        notion_token=user_ctx.notion_token)
        lines = [f"当前 {len(cats)} 个启用分类（来源：{src}）："]
        lines += [f"{i}. {c['name']}：{c.get('description', '')[:40]}" for i, c in enumerate(cats, 1)]
        await reply(chat_id, "\n".join(lines), message_id)
        return True
    if cmd == "/find":
        query = text.strip()[len("/find"):].strip()
        if not query:
            await reply(chat_id, "用法：/find 关键词（本地 FTS5 关键词检索，支持中英文）", message_id)
            return True
        store = get_store()
        hits = store.search_clips(query, tenant_id=user_ctx.user_id, limit=5)
        if not hits:
            await reply(chat_id, f"未在本地索引找到与「{query}」匹配的归档。", message_id)
            return True
        lines = [f"🔍 关键词「{query}」本地检索结果（Top {len(hits)}）："]
        for i, h in enumerate(hits, 1):
            t_str = " ".join(f"#{t}" for t in h["tags"][:4])
            lines.append(f"{i}. 【{h['category']}】{h['title']} {t_str}\n   {h['page_url']}")
        await reply(chat_id, "\n".join(lines), message_id)
        return True
    if cmd == "/recent":
        arg = text.strip()[len("/recent"):].strip()
        limit = min(20, max(1, int(arg))) if arg.isdigit() else 5
        hits = get_store().list_recent_clips(tenant_id=user_ctx.user_id, limit=limit)
        if not hits:
            await reply(chat_id, "暂无本地归档记录。", message_id)
            return True
        lines = [f"🕒 最近 {len(hits)} 条归档："]
        for i, h in enumerate(hits, 1):
            lines.append(f"{i}. 【{h['category']}】{h['title']} ({h['status']})\n   {h['page_url']}")
        await reply(chat_id, "\n".join(lines), message_id)
        return True
    if cmd == "/stats":
        st = get_store().stats(tenant_id=user_ctx.user_id, days=7)
        u = st["usage"]
        lines = [
            f"📊 近 {st['days']} 天归档统计（累计索引 {st['total_clips']} 条）",
            f"· 本周归档：{st['recent_clips']} 条 | 状态：{st['by_status'] or '无'}",
            f"· 抓取层级：{st['by_tier'] or '无'}",
            f"· Gemini 调用：{u.get('calls', 0)} 次 | 输入 {u.get('prompt_tokens', 0)} / 输出 {u.get('output_tokens', 0)} / 思考 {u.get('thinking_tokens', 0)} / 缓存 {u.get('cached_tokens', 0)}",
            f"· 估算模型费用：${u.get('cost_usd', 0.0):.4f}",
        ]
        if st["tag_suggestions"]:
            lines.append("· 标签纠偏高频建议：" + "；".join(st["tag_suggestions"]))
        await reply(chat_id, "\n".join(lines), message_id)
        return True
    if cmd == "/retag":
        parts = text.strip().split()
        if len(parts) < 3:
            await reply(chat_id, "用法：/retag <page_id> 新标签1 新标签2", message_id)
            return True
        target_id, new_tags = parts[1], clean_tags(parts[2:], limit=8)
        store = get_store()
        clip = store.get_clip(target_id, user_ctx.user_id)
        if not clip:
            await reply(chat_id, "未找到该页面或无权限修改。", message_id)
            return True
        updated = await update_page_tags(clip["page_id"], new_tags, scope=user_ctx.notion_scope())
        store.record_correction(tenant_id=user_ctx.user_id, clip_id=clip["id"], url=clip["canonical_url"],
                                title=clip["title"], old_tags=clip["tags"], new_tags=updated)
        await reply(chat_id, f"🏷️ 已更新标签为：{' '.join('#' + t for t in updated)}\n{clip['page_url']}", message_id)
        return True

    media = extract_media(message)
    if not media and not text.strip():
        await reply(chat_id, "支持文字、链接、图片、相册、语音、音频、视频和 PDF。", message_id)
        return True

    # #5 相册聚合路径
    if mgid := message.get("media_group_id"):
        async def mark_arrived(m_dict: dict) -> None:
            await set_reaction(chat_id, m_dict.get("message_id"), "👀")

        async def run_album(members: list[dict]) -> tuple[str, str]:
            placeholder, _ = await asyncio.gather(
                reply(chat_id, f"⏳ 正在聚合处理 {len(members)} 条相册素材…", members[0].get("message_id")),
                tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}),
            )
            try:
                async with _slots():
                    receipt, reaction = await _archive_album(members, user_ctx=user_ctx)
            except Exception as exc:
                log.error("相册归档失败：%s", type(exc).__name__)
                receipt, reaction = f"❌ 相册写入失败（{type(exc).__name__}）。请稍后重试。", "👎"
            for m_item in members:
                await set_reaction(chat_id, m_item.get("message_id"), reaction)
            edited = None
            if placeholder:
                edited = await tg_call("editMessageText", {
                    "chat_id": chat_id, "message_id": placeholder,
                    "text": truncate(receipt, 4000), "disable_web_page_preview": True,
                })
            if not edited:
                await reply(chat_id, receipt, members[0].get("message_id"))
            return receipt, reaction

        async def run_late(akey: str, late_msg: dict) -> tuple[str, str] | None:
            res = await _append_late_album_member(akey, late_msg, user_ctx=user_ctx)
            if res:
                await set_reaction(chat_id, late_msg.get("message_id"), res[1])
                await reply(chat_id, res[0], late_msg.get("message_id"))
            return res

        res = await album_coordinator.submit(
            user_id=user_ctx.user_id, chat_id=chat_id, media_group_id=str(mgid),
            message=message, on_ready=run_album, on_late=run_late, on_member_arrived=mark_arrived,
        )
        return True if res is None else (res[1] != "👎")

    await set_reaction(chat_id, message_id, "👀")
    placeholder, _ = await asyncio.gather(
        reply(chat_id, "⏳ 正在处理并准备归档…", message_id),
        tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}),
    )
    try:
        async with _slots():
            ctx_token = _active_user_ctx.set(user_ctx)
            try:
                receipt, reaction = await _archive_message(text, entities, message, media)
            finally:
                _active_user_ctx.reset(ctx_token)
    except Exception as exc:
        log.error("归档失败：%s", type(exc).__name__)
        receipt, reaction = f"❌ 写入失败（{type(exc).__name__}）。请稍后重新发送原消息。", "👎"
    await set_reaction(chat_id, message_id, reaction)
    keyboard = None
    if _maybe_store() and (page_url := _extract_receipt_notion_url(receipt)):
        keyboard = build_clip_keyboard(_clean_id(page_url), is_duplicate=(reaction == "👍"), has_url=("http" in text))
    edited = None
    if placeholder:
        edit_payload: dict = {"chat_id": chat_id, "message_id": placeholder,
                              "text": truncate(receipt, 4000), "disable_web_page_preview": True}
        if keyboard:
            edit_payload["reply_markup"] = keyboard
        edited = await tg_call("editMessageText", edit_payload)
    if not edited:
        await reply(chat_id, receipt, message_id, reply_markup=keyboard)
    return reaction != "👎"


async def process_update(update: dict) -> None:
    """Webhook 与本地轮询共用。失败或取消时释放 update ID，允许重新处理。"""
    update_id = update.get("update_id")
    if update_id is None:
        await _process_message(update)
        return
    if not await _claim_update(update_id):
        return
    completed = False
    try:
        completed = await _process_message(update)
    finally:
        await _finish_update(update_id, completed)


def configured_channels() -> list[str]:
    """列出已配置 API Key 的可选抓取渠道（不含任何密钥值）"""
    mapping = (("Tavily", "TAVILY_API_KEY"), ("Firecrawl", "FIRECRAWL_API_KEY"),
               ("ZenRows", "ZENROWS_API_KEY"), ("Jina", "JINA_API_KEY"), ("Reddit OAuth", "REDDIT_CLIENT_SECRET"))
    return [name for name, key in mapping if env(key)]


class IngestRequest(BaseModel):
    url: str = Field(default="", max_length=2000)
    text: str = Field(default="", max_length=20000)
    title: str = Field(default="", max_length=250)
    markdown: str = Field(default="", max_length=180000)
    annotation: str = Field(default="", max_length=8000)
    source_type: str = Field(default="ingest", max_length=40)
    user_id: int | None = None
    idempotency_key: str = Field(default="", max_length=128)
    async_queue: bool = False


async def _execute_ingest_payload(payload: IngestRequest, ctx: UserContext) -> dict:
    if payload.markdown:
        clip = BrowserClip(title=payload.title or payload.url or "快捷剪藏", url=payload.url,
                           mode="page", markdown=payload.markdown, annotation=payload.annotation)
        return await archive_browser_clip(clip, user_id=ctx.user_id)
    combined = "\n".join(filter(None, (payload.url, payload.annotation and f"批注：{payload.annotation}", payload.text)))
    receipt, reaction = await _archive_message(combined, [], {}, user_ctx=ctx)
    page_url = _extract_receipt_notion_url(receipt) or ""
    return {"ok": reaction != "👎", "url": page_url, "duplicate": reaction == "👍", "receipt": receipt}


async def process_pending_jobs(limit: int = 10) -> list[dict]:
    """#13 持久队列 Worker：指数退避重试与死信隔离。"""
    store = get_store()
    results = []
    for _ in range(limit):
        job = store.claim_next_job()
        if not job:
            break
        ctx = resolve_user_context(job["tenant_id"] if job["tenant_id"] else None)
        if ctx is None:
            store.fail_job(job["job_id"], "无效租户上下文", retryable=False)
            results.append({"job_id": job["job_id"], "status": "dead"})
            continue
        try:
            req = IngestRequest.model_validate_json(job["payload_json"])
            out = await _execute_ingest_payload(req, ctx)
            store.complete_job(job["job_id"], page_url=out.get("url", ""), fetch_tier="Ingest")
            results.append({"job_id": job["job_id"], "status": "done", "url": out.get("url", "")})
        except Exception as exc:
            status = store.fail_job(job["job_id"], f"{type(exc).__name__}: {exc}")
            results.append({"job_id": job["job_id"], "status": status})
    return results


@app.get("/")
async def health():
    return {"ok": True, "service": "tg-notion-clipper", "channels": configured_channels()}


@app.post("/webhook")
async def telegram_webhook(request: Request,
                           x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    secret = env("WEBHOOK_SECRET")
    if not secret or not hmac.compare_digest(x_telegram_bot_api_secret_token or "", secret):
        raise HTTPException(status_code=403, detail="Invalid secret token")
    update = await request.json()
    await process_update(update)  # 同步处理完再返回 200，按请求计费模式下 CPU 不会被冻结
    return {"ok": True}


@app.post("/clip")
async def post_browser_clip(request: Request):
    user_id = clip_token_user(request.headers.get("Authorization", ""))
    if user_id is None:
        raise HTTPException(status_code=401, detail="配对码无效或已过期")
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_CLIP_BYTES:
            raise HTTPException(status_code=413, detail="剪藏内容超过服务端接收上限")
        payload.extend(chunk)
    try:
        clip = BrowserClip.model_validate_json(payload)
    except ValidationError:
        raise HTTPException(status_code=422, detail="剪藏内容格式不正确或超出长度限制") from None
    try:
        async with _slots():
            return await archive_browser_clip(clip, user_id=user_id)
    except Exception as exc:
        log.warning("浏览器剪藏归档失败：%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="暂未归档，可重试或先下载 Markdown") from None


@app.post("/ingest")
async def post_ingest(payload: IngestRequest, request: Request,
                      x_ingest_secret: str | None = Header(default=None)):
    """#13 统一 Ingest API：供浏览器、RSS、快捷指令复用，统一鉴权、规范化与幂等。"""
    token_uid = clip_token_user(request.headers.get("Authorization", ""))
    secret = env("INGEST_SECRET") or env("WEBHOOK_SECRET")
    secret_ok = bool(secret and x_ingest_secret and hmac.compare_digest(x_ingest_secret, secret))
    if token_uid is None and not secret_ok:
        raise HTTPException(status_code=401, detail="鉴权失败")
    target_uid = token_uid if token_uid is not None else payload.user_id
    ctx = resolve_user_context(target_uid)
    if ctx is None:
        raise HTTPException(status_code=403, detail="未授权或未配置知识库的用户")
    if not (payload.url or payload.text or payload.markdown):
        raise HTTPException(status_code=422, detail="至少提供 url、text 或 markdown 之一")

    canon = normalize_url(payload.url) if payload.url else ""
    idem = payload.idempotency_key or hashlib.sha256(
        f"{ctx.user_id}:{canon}:{payload.title}:{payload.text[:200]}".encode("utf-8")
    ).hexdigest()[:32]
    if payload.async_queue:
        job, created = get_store().enqueue_job(
            tenant_id=ctx.user_id, idempotency_key=idem, source_type=payload.source_type,
            canonical_url=canon, payload=payload.model_dump(),
        )
        return {"ok": True, "queued": True, "created": created, "job_id": job["job_id"], "status": job["status"]}
    async with _slots():
        return await _execute_ingest_payload(payload, ctx)


@app.post("/jobs/run")
async def post_run_jobs(limit: int = 10, x_ingest_secret: str | None = Header(default=None),
                        x_telegram_bot_api_secret_token: str | None = Header(default=None)):
    secret = env("INGEST_SECRET") or env("WEBHOOK_SECRET")
    provided = x_ingest_secret or x_telegram_bot_api_secret_token or ""
    if not secret or not hmac.compare_digest(provided, secret):
        raise HTTPException(status_code=403, detail="Invalid secret token")
    processed = await process_pending_jobs(max(1, min(50, limit)))
    return {"ok": True, "processed": processed}
