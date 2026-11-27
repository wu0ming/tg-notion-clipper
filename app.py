"""
Description: Telegram Bot Webhook 入口（部署于 Google Cloud Run）+ 本地轮询共用的消息处理流水线
Changelog:
- v1.0 (2026-11): 方案原始版本（BackgroundTasks + --no-cpu-throttling）
- v1.3 (2026-11): 抓取与 Gemini 之前先按 URL 查重；关闭 httpx 请求日志防止 Bot Token 泄露
- v1.2 (2026-11): 单库写入；新增 /reload /categories 命令（分类来自 Notion「分类配置」表）
- v1.1 (2026-11): 改为请求内同步处理。原因：--no-cpu-throttling 会切换为"实例全生命周期计费"，空闲实例也计费，
  个人低频场景反而容易超出免费额度；默认"按请求计费"下请求结束后 CPU 会被冻结，后台任务不可靠。
  同时增加 update_id 去重（防 Telegram 重试重复处理）、/start /help /id 命令、图片/转发消息的 caption 支持、文字超链接识别
- v2.0 (2026-11): 回执增加抓取渠道信息；FastAPI lifespan 管理 Notion 连接池生命周期
"""
import asyncio
import hmac
import json
import logging
import os
import re
from collections import OrderedDict
from contextlib import asynccontextmanager
from weakref import WeakValueDictionary

import httpx
from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import ValidationError

from config import env, allowed_user_ids
from utils import URL_RE, clean_tags, extract_url, truncate, normalize_url
from router import smart_fetch
from gemini_processor import AnalysisResult, analyze_content, match_category, recover_web
from category_config import get_categories
from notion_writer import save_to_notion, find_existing, close_client, append_telegram_note
from telegram_media import TelegramMedia, download_media, extract_media
from telegram_source import explicit_note, forward_source
from browser_clipper import BrowserClip, MAX_CLIP_BYTES, archive_browser_clip, authorized_clip, issue_clip_token


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
HELP_TEXT = ("把链接、文字、图片、语音、音频或 PDF 发给我，我会分类打标并归档到 Notion。\n"
             "· 链接或附件可附上批注和 #标签。附件单个最大 20 MB。\n"
             "· 回复自己已归档的原消息并以「批注：」开头，可补充个人批注。\n"
             "· 👀 处理中 · 👌 已归档 · 👍 已收录 · 🤔 降级留存 · 👎 写入失败\n"
             "· /categories 查看当前分类；/reload 立即重新读取 Notion「分类配置」表\n"
             "· /clipkey 获取浏览器剪藏配对码；/id 查看你的 Telegram 数字 ID")


async def tg_call(method: str, payload: dict) -> dict | None:
    token = env("TELEGRAM_BOT_TOKEN")
    try:
        client = _get_tg_client()
        response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload,
                                     timeout=2.0 if method in {"setMessageReaction", "sendChatAction"} else 5.0)
        data = response.json()
        if response.is_success and data.get("ok"):
            return data
        log.warning("Telegram %s 调用失败：HTTP %s", method, response.status_code)
    except Exception as exc:
        log.warning("Telegram %s 调用失败：%s", method, type(exc).__name__)
    return None


async def reply(chat_id: int, text: str, reply_to: int | None = None) -> int | None:
    payload = {"chat_id": chat_id, "text": truncate(text, 4000), "disable_web_page_preview": True}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    response = await tg_call("sendMessage", payload)
    return (response.get("result") or {}).get("message_id") if response else None


async def set_reaction(chat_id: int, message_id: int | None, emoji: str) -> None:
    if message_id:
        await tg_call("setMessageReaction", {"chat_id": chat_id, "message_id": message_id,
                                              "reaction": [{"type": "emoji", "emoji": emoji}]})


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


async def _find_duplicate(url: str) -> str | None:
    if not url:
        return None
    try:
        existing = await find_existing(url)
        if existing or not env("NOTION_DB_INBOX"):
            return existing
        return await find_existing(url, inbox=True)
    except Exception as exc:
        log.warning("Notion 预查重失败：%s", type(exc).__name__)
        return None


def _duplicate_receipt(url: str) -> tuple[str, str]:
    return f"♻️ 该链接已归档过，跳过重复写入：\n{url}", "👍"


async def _archive_message(text: str, entities: list | None, message: dict,
                           media: TelegramMedia | None = None) -> tuple[str, str]:
    forward = forward_source(message)
    raw_url, accompanying = extract_url(text, entities)
    if not raw_url or media:
        return await _archive_clip(text, message, media, forward, raw_url, accompanying)

    try:
        url = normalize_url(raw_url)
    except ValueError:
        url = ""
    has_new_material = bool(message.get("message_id") and (forward or accompanying))
    if not url or has_new_material:
        return await _archive_clip(text, message, media, forward, raw_url, accompanying,
                                   url=url, deduplicate_by_url=not has_new_material)
    key = (asyncio.get_running_loop(), url)
    lock = _url_locks.setdefault(key, asyncio.Lock())
    async with lock:
        return await _archive_clip(text, message, media, forward, raw_url, accompanying, url=url)


async def _archive_clip(text: str, message: dict, media: TelegramMedia | None, forward: dict,
                        raw_url: str | None, accompanying: str, *, url: str = "",
                        deduplicate_by_url: bool = True) -> tuple[str, str]:
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
    analyze = True
    fetched_model_content = None
    reference_links = ()

    if media:
        source, title, input_type, tier = "Telegram", media.file_name, media.input_type, "Direct-Multimodal"
        content = ""
        try:
            media_bytes = await download_media(_get_tg_client(), env("TELEGRAM_BOT_TOKEN"), media)
            attachment = (media_bytes, media.mime_type, media.file_name)
        except ValueError as exc:
            note = f"附件未下载：{exc}"
            status, tier, analyze = "FALLBACK", "Tier4-Fallback", False
        except Exception as exc:
            note = f"附件下载失败（{type(exc).__name__}）"
            status, tier, analyze = "FALLBACK", "Tier4-Fallback", False
    elif raw_url:
        input_type = "Link"
        if deduplicate_by_url and (duplicate := await _find_duplicate(url)):
            return _duplicate_receipt(duplicate)
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
                if deduplicate_by_url and (duplicate := await _find_duplicate(url)):
                    return _duplicate_receipt(duplicate)
        if status == "NEEDS_GEMINI_WEB":
            try:
                web_title, web_content, status, web_note = await recover_web(url, user_note)
            except Exception as exc:
                web_title, web_content, status, web_note = "", "", "FALLBACK", f"Gemini 联网失败（{type(exc).__name__}）"
            note = "；".join(filter(None, (note, web_note)))
            if status in {"PASS", "PARTIAL"} and web_content:
                title, content, channel, tier = web_title or title, web_content, "Gemini 联网", "Tier3-GeminiWeb"
            else:
                status = "FALLBACK"
        if status == "FALLBACK":
            tier, analyze = "Tier4-Fallback", False
    if analyze:
        model_content = content if fetched_model_content is None else fetched_model_content
        if forward and text:
            model_content = "\n\n".join(filter(None, (f"【Telegram 转发内容】\n{text}", model_content)))
        try:
            result, _, model = await analyze_content(source, title, model_content, user_note, status,
                                                     media_bytes=media_bytes, media_mime=media.mime_type if media else None,
                                                     input_type=input_type, user_hashtags=user_tags)
        except Exception as exc:
            log.warning("Gemini 分类暂不可用：%s", type(exc).__name__)
            note = "；".join(filter(None, (note, f"AI 分类暂不可用（{type(exc).__name__}）")))
            status, tier, analyze = "AI_PENDING", "Tier4-Fallback", False

    if not analyze:
        categories, _ = await get_categories()
        fallback = match_category(categories, "")
        summary = "AI 分类暂不可用，已留存原始素材，待整理。" if status == "AI_PENDING" else "原始内容暂时无法读取，已留存可用素材。"
        result = AnalysisResult(title=truncate(title or text.strip() or url or "待整理归档", 40),
                                category=fallback["name"], tags=clean_tags([*user_tags, "待整理"], limit=5),
                                summary=summary, key_points=[])

    inbox = status in {"FALLBACK", "AI_PENDING"}
    media_metadata = media.metadata(message) if media else None
    if media_metadata is not None and not media_bytes:
        media_metadata["archive_note"] = "附件保留在 Telegram，未能下载原文件。"
    page_url, is_dup = await save_to_notion(result, source, url, status, content, user_note, note, inbox=inbox,
                                           input_type=input_type, fetch_tier=tier, original_url=raw_url or "",
                                           media_metadata=media_metadata, attachment=attachment,
                                           telegram_message={"chat_id": (message.get("chat") or {}).get("id"),
                                                             "message_id": message.get("message_id")},
                                           forwarded_text=text if forward else "", forward_metadata=forward,
                                           accompanying_text=accompanying_text,
                                           deduplicate_by_url=deduplicate_by_url, reference_links=reference_links)
    log.info("archived source=%s status=%s model=%s tier=%s channel=%s dup=%s", source, status, model, tier, channel, is_dup)

    if is_dup:
        return _duplicate_receipt(page_url)
    degraded = tier in {"Tier3-GeminiWeb", "Tier4-Fallback"}
    icon = "🟠" if degraded or status == "PARTIAL" else "✅"
    tags = " ".join(f"#{tag.replace(' ', '_')}" for tag in result.tags)
    lines = [f"{icon} 已归档至【{result.category}】（{source} · {status}）",
             f"标题：{result.title}", f"标签：{tags}", f"摘要：{result.summary}"]
    if inbox and env("NOTION_DB_INBOX"):
        lines.append("去向：Notion 收件箱")
    if note:
        lines.append(f"提示：{truncate(note, 700)}")
    if channel:
        lines.append(f"渠道：{channel}")
    if tier:
        lines.append(f"抓取层级：{tier}")
    lines.append(f"Notion：{page_url}")
    return "\n".join(lines), "🤔" if degraded else "👌"


async def archive_text(text: str, entities: list | None) -> str:
    receipt, _ = await _archive_message(text, entities, {})
    return receipt


async def _handle_note_reply(message: dict, target: dict, note: str) -> bool:
    chat_id, message_id = message["chat"]["id"], message.get("message_id")
    target_id = target.get("message_id")
    if not note or not isinstance(message_id, int) or not isinstance(target_id, int) or (
        (target.get("chat") or {}).get("id") != chat_id or
        (target.get("from") or {}).get("id") != (message.get("from") or {}).get("id")
    ):
        await reply(chat_id, "请回复自己在当前聊天里已归档的原消息，并在「批注：」后填写内容。", message_id)
        return True
    try:
        page_url = await append_telegram_note(chat_id, target_id, message_id, note)
    except Exception as exc:
        log.warning("Telegram 批注写入失败：%s", type(exc).__name__)
        await reply(chat_id, "批注暂未写入，请稍后重试。", message_id)
        return False
    receipt = f"个人批注已保存\n{page_url}" if page_url else "找不到这条原消息对应的归档页面。"
    await reply(chat_id, receipt, message_id)
    return True


async def _process_message(update: dict) -> bool:
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
    if user_id not in allowed_user_ids():
        log.info("忽略非白名单用户 %s", user_id)
        return True
    if not forwarded and message.get("text") and (target := message.get("reply_to_message")):
        if (note := explicit_note(text)) is not None:
            return await _handle_note_reply(message, target, note)
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
        cats, src = await get_categories(force=(cmd == "/reload"))
        lines = [f"当前 {len(cats)} 个启用分类（来源：{src}）："]
        lines += [f"{i}. {c['name']}：{c.get('description', '')[:40]}" for i, c in enumerate(cats, 1)]
        await reply(chat_id, "\n".join(lines), message_id)
        return True

    media = extract_media(message)
    if not media and not text.strip():
        await reply(chat_id, "支持文字、链接、图片、语音、音频和 PDF。", message_id)
        return True

    await set_reaction(chat_id, message_id, "👀")
    placeholder, _ = await asyncio.gather(
        reply(chat_id, "⏳ 正在处理并准备归档…", message_id),
        tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"}),
    )
    try:
        async with _slots():
            receipt, reaction = await _archive_message(text, entities, message, media)
    except Exception as exc:
        log.error("归档失败：%s", type(exc).__name__)
        receipt, reaction = f"❌ 写入失败（{type(exc).__name__}）。请稍后重新发送原消息。", "👎"
    await set_reaction(chat_id, message_id, reaction)
    edited = None
    if placeholder:
        edited = await tg_call("editMessageText", {"chat_id": chat_id, "message_id": placeholder,
                                                   "text": truncate(receipt, 4000), "disable_web_page_preview": True})
    if not edited:
        await reply(chat_id, receipt, message_id)
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
    if not authorized_clip(request.headers.get("Authorization", "")):
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
            return await archive_browser_clip(clip)
    except Exception as exc:
        log.warning("浏览器剪藏归档失败：%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="暂未归档，可重试或先下载 Markdown") from None
