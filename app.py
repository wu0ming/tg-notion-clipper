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
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Header, HTTPException

from config import env, allowed_user_ids
from utils import extract_url, truncate, normalize_url
from router import smart_fetch
from gemini_processor import analyze_content
from category_config import get_categories
from notion_writer import save_to_notion, find_existing, close_client


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
_lock = asyncio.Lock()
HELP_TEXT = ("把任意链接或文字发给我，我会自动抓取、分类打标并归档到 Notion。\n"
             "· 链接 + 一句批注：批注会作为高优先级分类参考并写入页面\n"
             "· 支持：X/Twitter、Reddit、V2EX、微信公众号、通用网页、纯文本随手记\n"
             "· /categories 查看当前分类；/reload 立即重新读取 Notion「分类配置」表\n"
             "· /id 查看你的 Telegram 数字 ID")


async def tg_call(method: str, payload: dict) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    try:
        client = _get_tg_client()
        await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=10.0)
    except Exception as exc:
        log.warning("Telegram %s 调用失败：%s", method, exc)


async def reply(chat_id: int, text: str, reply_to: int | None = None) -> None:
    payload = {"chat_id": chat_id, "text": truncate(text, 4000), "disable_web_page_preview": True}
    if reply_to:
        payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
    await tg_call("sendMessage", payload)


async def _already_seen(update_id: int) -> bool:
    async with _lock:
        if update_id in _seen_updates:
            return True
        _seen_updates[update_id] = None
        while len(_seen_updates) > 500:
            _seen_updates.popitem(last=False)
        return False


async def archive_text(text: str, entities: list | None) -> str:
    """核心流水线：抓取 → Gemini 分类打标 → Notion 写入，返回给用户的回执文本"""
    raw_url, user_note = extract_url(text, entities)
    channel = ""
    if raw_url:
        # 先按归一化链接查重：重复链接不再抓取、不再调用 Gemini（实测可省约 20 秒和一次模型费用）
        try:
            dup = await find_existing(normalize_url(raw_url))
        except Exception as exc:
            log.warning("预查重失败，继续正常流程：%s", exc)
            dup = None
        if dup:
            log.info("archived dup=True (pre-check)")
            return f"♻️ 该链接已归档过，跳过重复写入：\n{dup}"
        fetched = await smart_fetch(raw_url)
        source, url, title, content, status, note = (fetched.source_type, fetched.clean_url, fetched.title,
                                                     fetched.content, fetched.status, fetched.note)
        channel = fetched.channel
        if url != normalize_url(raw_url):  # 短链展开后得到的新地址再查一次
            try:
                dup = await find_existing(url)
            except Exception:
                dup = None
            if dup:
                return f"♻️ 该链接已归档过，跳过重复写入：\n{dup}"
    else:
        source, url, title, content, status, note = "Note", "", "随手记", text, "PASS", ""
        user_note = ""

    result, rule, model = await analyze_content(source, title, content, user_note, status)
    page_url, is_dup = await save_to_notion(result, source, url, status, content, user_note, note)
    log.info("archived source=%s status=%s category=%s model=%s channel=%s dup=%s",
             source, status, result.category, model, channel, is_dup)

    if is_dup:
        return f"♻️ 该链接已归档过，跳过重复写入：\n{page_url}"
    icon = {"PASS": "✅", "PARTIAL": "🟠", "FALLBACK": "🔴"}.get(status, "✅")
    tags = " ".join(f"#{t.replace(' ', '_')}" for t in result.tags)
    lines = [f"{icon} 已归档至【{result.category}】（{source} · {status}）",
             f"标题：{result.title}", f"标签：{tags}", f"摘要：{result.summary}"]
    if note:
        lines.append(f"提示：{note}")
    if channel:
        lines.append(f"渠道：{channel}")
    lines.append(f"Notion：{page_url}")
    return "\n".join(lines)


async def process_update(update: dict) -> None:
    """Webhook 与本地轮询共用的单条 update 处理入口"""
    update_id = update.get("update_id")
    if update_id is not None and await _already_seen(update_id):
        return
    message = update.get("message") or update.get("edited_message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    user_id = (message.get("from") or {}).get("id")
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []

    if text.strip().startswith("/id"):
        await reply(chat_id, f"你的 Telegram 数字 ID：{user_id}", message.get("message_id"))
        return
    if user_id not in allowed_user_ids():
        log.info("忽略非白名单用户 %s", user_id)
        return  # 静默忽略，防止他人盗刷额度
    cmd = text.strip().split()[0].split("@")[0].lower() if text.strip().startswith("/") else ""
    if cmd in ("/start", "/help"):
        await reply(chat_id, HELP_TEXT)
        return
    if cmd in ("/reload", "/categories"):
        cats, src = await get_categories(force=(cmd == "/reload"))
        lines = [f"当前 {len(cats)} 个启用分类（来源：{src}）："]
        lines += [f"{i}. {c['name']}：{c.get('description', '')[:40]}" for i, c in enumerate(cats, 1)]
        await reply(chat_id, "\n".join(lines), message.get("message_id"))
        return
    if not text.strip():
        await reply(chat_id, "暂只支持文字/链接（图片和语音识别在二期计划中）。", message.get("message_id"))
        return

    await tg_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    try:
        msg = await archive_text(text, entities)
    except Exception as exc:
        log.exception("处理失败")
        msg = f"❌ 处理失败：{type(exc).__name__}: {str(exc)[:500]}"
    await reply(chat_id, msg, message.get("message_id"))


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
    if not hmac.compare_digest(x_telegram_bot_api_secret_token or "", env("WEBHOOK_SECRET")):
        raise HTTPException(status_code=403, detail="Invalid secret token")
    update = await request.json()
    await process_update(update)  # 同步处理完再返回 200，按请求计费模式下 CPU 不会被冻结
    return {"ok": True}
