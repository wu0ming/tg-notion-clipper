"""
Description: Notion 归档、收件箱与原始媒体留存，直接调用官方 REST API
Changelog:
- v1.0 (2026-11): 方案原始版本（notion-client databases.query，按分类分库）
- v1.1 (2026-11): httpx 直连 REST API，固定 Notion-Version 2025-09-03，自动由 database_id 解析 data_source_id
- v1.2 (2026-11): 改为单库：所有内容写入「归档总表」（NOTION_DB_ARCHIVE），Category 下拉框是唯一分类依据；
  去重范围随之扩大到全部分类
- v2.0 (2026-11): 修复缺少 import asyncio 的 bug（429/5xx 重试时 asyncio.sleep 会报 NameError）；
  改为模块级 httpx.AsyncClient 连接池，减少重复 TLS 握手
"""
import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx

from config import env
from utils import build_properties, build_children, chunk_text, rich_text

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
_ds_cache: dict[str, str] = {}
_schema_cache: dict[str, tuple[float, dict]] = {}
_write_locks: dict[tuple[asyncio.AbstractEventLoop, str], tuple[asyncio.Lock, int]] = {}
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None
log = logging.getLogger("clipper")
MARKDOWN_CHILD_TITLE = "剪藏原文"
_MARKDOWN_TARGET = r'(?:\\.|[^()[\]\n]|\((?:\\.|[^()\n])*\))+'
_MARKDOWN_IMAGE = rf'!\[(?P<alt>(?:\\.|[^\]\\\n])*)\]\({_MARKDOWN_TARGET}\)'
_MARKDOWN_IMAGE_TOKEN = re.compile(
    rf'(?<!\\)(?:\[(?P<linked>{_MARKDOWN_IMAGE})\]\((?P<href>{_MARKDOWN_TARGET})\)|(?P<standalone>!\[(?:\\.|[^\]\\\n])*\]\({_MARKDOWN_TARGET}\)))'
)
_MEDIA_FIELDS = ("chat_id", "message_id", "file_id", "file_unique_id", "mime_type", "mime", "filename",
                 "file_name", "file_size", "size", "duration", "duration_seconds", "date", "media_group_id",
                 "telegram_url", "archive_note")


class NotionError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _get_client() -> httpx.AsyncClient:
    """获取当前事件循环对应的 httpx 连接池，既复用到 api.notion.com 的连接，又避免跨 asyncio.run() 报 loop closed"""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient()
        _client_loop = loop
    return _client


async def close_client() -> None:
    """关闭模块级连接池（由 app.py lifespan 调用）"""
    global _client, _client_loop
    if _client and not _client.is_closed:
        await _client.aclose()
    _client = None
    _client_loop = None


def _headers() -> dict:
    return {"Authorization": f"Bearer {env('NOTION_TOKEN')}", "Notion-Version": NOTION_VERSION}


def _clean_id(raw: str) -> str:
    raw = raw.strip().split("?")[0].rstrip("/").split("/")[-1]
    return raw.split("-")[-1] if len(raw) > 32 and "-" in raw and len(raw.split("-")[-1]) == 32 else raw


async def _request(client: httpx.AsyncClient, method: str, path: str, *, confirm_created=None, **kwargs) -> dict:
    """查询可重试。创建页面结果不明时查重确认，避免再次提交时创建副本。"""
    last = None
    for attempt in range(4):
        wait = 2 ** attempt
        uncertain = False
        try:
            resp = await client.request(method, f"{NOTION_API}{path}", headers=_headers(), timeout=30.0, **kwargs)
        except httpx.TransportError as exc:
            last = NotionError(f"Notion 网络错误：{type(exc).__name__}")
            uncertain = not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
        else:
            if resp.is_success:
                return resp.json()
            try:
                detail = resp.json().get("message", resp.text)
            except (ValueError, AttributeError):
                detail = resp.text
            last = NotionError(f"Notion {resp.status_code}：{str(detail)[:300]}", resp.status_code)
            if resp.status_code != 429 and resp.status_code < 500:
                raise last
            uncertain = resp.status_code >= 500
            try:
                wait = float(resp.headers.get("retry-after", wait))
            except ValueError:
                pass
        is_non_idempotent = (method == "POST" and path == "/pages") or (
            method == "PATCH" and path.startswith("/blocks/") and path.endswith("/children")
        )
        if is_non_idempotent and uncertain:
            if confirm_created is not None:
                await asyncio.sleep(1)
                if page := await confirm_created():
                    return page
            raise last
        if attempt < 3:
            await asyncio.sleep(max(0.0, min(wait, 10.0)))
    raise last or NotionError("Notion 请求失败")


async def resolve_data_source_id(client: httpx.AsyncClient, database_or_ds_id: str) -> str:
    """环境变量里填数据库 ID（浏览器地址栏那 32 位）或 data source ID 均可"""
    key = _clean_id(database_or_ds_id)
    if key in _ds_cache:
        return _ds_cache[key]
    try:
        db = await _request(client, "GET", f"/databases/{key}")
        sources = db.get("data_sources") or []
        if not sources:
            raise NotionError("该数据库下没有 data source")
        ds_id = sources[0]["id"]
    except NotionError as exc:
        if exc.code in (400, 404):
            schema = await _request(client, "GET", f"/data_sources/{key}")  # 本身就是 data source ID
            _schema_cache[key] = (time.monotonic(), schema.get("properties", {}))
            ds_id = key
        else:
            raise
    _ds_cache[key] = ds_id
    return ds_id


async def _data_source_schema(client: httpx.AsyncClient, ds_id: str) -> dict:
    cached_at, schema = _schema_cache.get(ds_id, (0, {}))
    if time.monotonic() - cached_at >= 300 or ds_id not in _schema_cache:
        data = await _request(client, "GET", f"/data_sources/{ds_id}")
        schema = data.get("properties", {})
        _schema_cache[ds_id] = (time.monotonic(), schema)
    return schema


def _destination(inbox: bool) -> str:
    return (env("NOTION_DB_INBOX") if inbox else "") or env("NOTION_DB_ARCHIVE")


def _lookup_filter(schema: dict, url: str, telegram_key: str = "", clip_key: str = "") -> dict | None:
    has_clip_key = schema.get("ClipKey", {}).get("type") == "rich_text"
    if clip_key:
        return {"property": "ClipKey", "rich_text": {"equals": clip_key}} if has_clip_key else None
    filters = []
    if url and schema.get("URL", {}).get("type") == "url":
        url_filter = {"property": "URL", "url": {"equals": url[:2000]}}
        if has_clip_key:
            url_filter = {"and": [url_filter, {"property": "ClipKey", "rich_text": {"is_empty": True}}]}
        filters.append(url_filter)
    if telegram_key and schema.get("TelegramKey", {}).get("type") == "rich_text":
        filters.append({"property": "TelegramKey", "rich_text": {"equals": telegram_key}})
    if len(filters) > 1:
        return {"or": filters}
    return next(iter(filters), None)


async def _lookup_page(client: httpx.AsyncClient, ds_id: str, page_filter: dict | None) -> dict | None:
    if page_filter:
        found = await _request(client, "POST", f"/data_sources/{ds_id}/query",
                               json={"filter": page_filter, "page_size": 1})
        return next(iter(found.get("results") or []), None)
    return None


async def find_existing(url: str, *, inbox: bool = False, clip_key: str = "") -> str | None:
    """按归一化 URL 在指定归档库查重。未配置独立收件箱时沿用归档总表。"""
    db_id = _destination(inbox)
    if not url or not db_id:
        return None
    client = _get_client()
    ds_id = await resolve_data_source_id(client, db_id)
    schema = await _data_source_schema(client, ds_id)
    page = await _lookup_page(client, ds_id, _lookup_filter(schema, url, clip_key=clip_key))
    return page["url"] if page else None


@asynccontextmanager
async def _serialized_write(key: str):
    if not key:
        yield
        return
    lock_key = (asyncio.get_running_loop(), key)
    lock, count = _write_locks.get(lock_key, (None, 0))
    lock = lock or asyncio.Lock()
    _write_locks[lock_key] = (lock, count + 1)
    try:
        async with lock:
            yield
    finally:
        _, count = _write_locks[lock_key]
        if count == 1:
            del _write_locks[lock_key]
        else:
            _write_locks[lock_key] = (lock, count - 1)


def _split_markdown_images(markdown: str) -> str:
    """Notion's Markdown parser requires each image to occupy its own block."""
    fence = ""
    output = []
    for line in markdown.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1].startswith(fence) and not marker[2].strip():
                fence = ""
        elif marker:
            fence = marker[1]
        elif "![" in line and not line.startswith(("    ", "\t")):
            def separate(match: re.Match) -> str:
                if not match["linked"] and match[0] == line.strip():
                    return match[0]
                image = match["linked"] or match["standalone"]
                link = f"\n\n[{match['alt'] or '图片链接'}]({match['href']})" if match["linked"] else ""
                return f"\n\n{image}{link}\n\n"

            line = _MARKDOWN_IMAGE_TOKEN.sub(separate, line)
        output.append(line)
    return "".join(output)


async def ensure_markdown_child(parent_url: str, markdown: str) -> None:
    """Create the Markdown as a native child page and repair partial browser captures on retry."""
    parent_id = _clean_id(parent_url)
    client = _get_client()

    async def find_child() -> dict | None:
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = await _request(client, "GET", f"/blocks/{parent_id}/children", params=params)
            for block in data.get("results", []):
                if block.get("type") == "child_page" and block["child_page"].get("title") == MARKDOWN_CHILD_TITLE:
                    return block
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor:
                return None

    async with _serialized_write(f"markdown-child:{parent_id}"):
        if await find_child():
            return
        body = {
            "parent": {"type": "page_id", "page_id": parent_id},
            "properties": {"title": {"type": "title", "title": rich_text(MARKDOWN_CHILD_TITLE)}},
            "markdown_version": "v2",
            "markdown": markdown,
        }
        try:
            await _request(client, "POST", "/pages", confirm_created=find_child, json=body)
        except NotionError as exc:
            if exc.code != 400 or "Images must be standalone blocks" not in str(exc):
                raise
            separated = _split_markdown_images(markdown)
            if separated == markdown:
                raise
            body["markdown"] = separated
            await _request(client, "POST", "/pages", confirm_created=find_child, json=body)


def _telegram_key(metadata: dict) -> str:
    chat_id, message_id = metadata.get("chat_id"), metadata.get("message_id")
    return f"{chat_id}:{message_id}" if isinstance(chat_id, int) and isinstance(message_id, int) else ""


async def append_telegram_note(chat_id: int, target_id: int, message_id: int, note: str) -> str | None:
    """Use the archived message mapping and a stable marker to avoid duplicating a reply."""
    client = _get_client()
    telegram_key = _telegram_key({"chat_id": chat_id, "message_id": target_id})
    page = None
    databases = dict.fromkeys(_clean_id(db) for db in (_destination(False), _destination(True)) if db)
    for db in databases:
        ds_id = await resolve_data_source_id(client, db)
        schema = await _data_source_schema(client, ds_id)
        page = await _lookup_page(client, ds_id, _lookup_filter(schema, "", telegram_key))
        if page:
            break
    if not page:
        return None

    path = f"/blocks/{page['id']}/children"
    title = f"个人批注 · Telegram #{message_id}"

    async def find_note() -> dict | None:
        params = {"page_size": 100}
        while True:
            data = await _request(client, "GET", path, params=params)
            for block in data.get("results", []):
                if block.get("type") != "heading_2":
                    continue
                parts = block["heading_2"].get("rich_text", [])
                if "".join(part.get("plain_text") or part.get("text", {}).get("content", "") for part in parts) == title:
                    return block
            cursor = data.get("next_cursor")
            if not cursor:
                return None
            params["start_cursor"] = cursor

    async with _serialized_write(f"telegram-note:{page['id']}:{message_id}"):
        if not await find_note():
            await _request(client, "PATCH", path, confirm_created=find_note,
                           json={"children": _text_section(title, note)})
    return page["url"]


def _fit_properties(properties: dict, schema: dict) -> dict:
    """收件箱可使用较精简的 schema，保留其实际标题字段及支持的属性。"""
    fitted = {}
    for name, prop in schema.items():
        prop_type = prop.get("type")
        if prop_type == "title":
            fitted[name] = properties["Name"]
        elif prop_type in properties.get(name, {}):
            fitted[name] = properties[name]
        elif name == "Status" and prop_type == "status":
            desired = properties[name]["select"]["name"]
            options = {item.get("name") for item in prop["status"].get("options", [])}
            if desired in options:
                fitted[name] = {"status": {"name": desired}}
            elif desired == "AI_PENDING" and "FALLBACK" in options:
                fitted[name] = {"status": {"name": "FALLBACK"}}
    return fitted


def _text_section(title: str, content: str) -> list[dict]:
    pieces = chunk_text(content, 1800)[:24]
    if not pieces:
        return []
    return [{"object": "block", "type": "heading_2", "heading_2": {
        "rich_text": rich_text(title), "is_toggleable": True,
        "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(piece)}}
                     for piece in pieces]}}]


async def _upload_attachment(client: httpx.AsyncClient, attachment: tuple[bytes, str, str]) -> dict:
    content, mime_type, filename = attachment
    filename = re.sub(r"[\x00-\x1f]", "", filename.replace("\\", "/").split("/")[-1]).strip() or "telegram-media"
    # Notion 文件名限制按字节计算。保留扩展名供 MIME 类型校验。
    if len(filename.encode("utf-8")) > 900:
        stem, dot, ext = filename.rpartition(".")
        filename = (stem or filename).encode("utf-8")[:800].decode("utf-8", "ignore") + (dot + ext[:20] if stem else "")
    upload = await _request(client, "POST", "/file_uploads", json={})
    uploaded = await _request(client, "POST", f"/file_uploads/{upload['id']}/send",
                              files={"file": (filename, content, mime_type)})
    if uploaded.get("status") != "uploaded":
        raise NotionError("Notion 未确认原始媒体上传")
    return {"object": "block", "type": "file", "file": {
        "type": "file_upload", "file_upload": {"id": upload["id"]}, "caption": rich_text("Telegram 原始附件")}}


async def save_to_notion(result, source_type: str, url: str, status: str, raw_content: str,
                         user_note: str = "", fetch_note: str = "", *, inbox: bool = False,
                         input_type: str = "", fetch_tier: str = "", original_url: str = "",
                         media_metadata: dict | None = None,
                         attachment: tuple[bytes, str, str] | None = None,
                         clip_key: str = "", telegram_message: dict | None = None,
                         forwarded_text: str = "", forward_metadata: dict | None = None,
                         accompanying_text: str = "") -> tuple[str, bool]:
    """写入归档总表或收件箱，返回 (Notion 页面链接, 是否重复)。"""
    db_id = _destination(inbox)
    if not db_id:
        raise NotionError("未配置 NOTION_DB_ARCHIVE（归档总表 ID），请先运行 python setup_notion.py")

    client = _get_client()
    ds_id = await resolve_data_source_id(client, db_id)
    schema = await _data_source_schema(client, ds_id)
    media = media_metadata or {}
    telegram_key = _telegram_key(telegram_message or media)
    page_filter = _lookup_filter(schema, url, telegram_key, clip_key)

    async def find_page() -> dict | None:
        return await _lookup_page(client, ds_id, page_filter)

    async with _serialized_write(clip_key or url or telegram_key):
        if existing := await find_page():
            return existing["url"], True
        other_db = _destination(not inbox)
        if other_db and _clean_id(other_db) != _clean_id(db_id) and (url or telegram_key):
            try:
                other_ds = await resolve_data_source_id(client, other_db)
                other_schema = await _data_source_schema(client, other_ds)
                existing = await _lookup_page(client, other_ds, _lookup_filter(other_schema, url, telegram_key, clip_key))
                if existing:
                    return existing["url"], True
            except NotionError as exc:
                log.warning("Notion 跨库查重失败（HTTP %s）", exc.code or "未收到响应")

        properties = build_properties(result.title, result.category, result.tags, source_type, url,
                                      result.summary, status, datetime.now(timezone.utc).isoformat())
        if input_type:
            properties["InputType"] = {"select": {"name": input_type}}
        if fetch_tier:
            properties["FetchTier"] = {"select": {"name": fetch_tier}}
        if telegram_key:
            properties["TelegramKey"] = {"rich_text": rich_text(telegram_key)}
        if clip_key:
            properties["ClipKey"] = {"rich_text": rich_text(clip_key)}
        properties = _fit_properties(properties, schema)

        children = build_children(result.key_points[:12], raw_content[:36000],
                                  "" if accompanying_text else user_note, url, status, fetch_note)
        children += _text_section("随附说明（来源未确认）", accompanying_text)
        children += _text_section("Telegram 转发原文", forwarded_text)
        if forward_metadata:
            children += _text_section("Telegram 转发来源", "\n".join(
                f"{key}：{value}" for key, value in forward_metadata.items() if key != "链接"))
            if forward_url := forward_metadata.get("链接"):
                children.append({"object": "block", "type": "bookmark", "bookmark": {"url": forward_url}})
        if result.summary and "Summary" not in properties:
            children += _text_section("摘要", result.summary)
        if input_type or fetch_tier:
            details = " · ".join(filter(None, (input_type, fetch_tier)))
            children += [{"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": rich_text(f"输入与归档渠道：{details}")}}]
        if original_url and (original_url != url or len(original_url) > 2000):
            children += _text_section("原始链接", original_url)
        if fetch_note:
            children += _text_section("处理记录", fetch_note)
        children += _text_section("提取文字 / 语音转写", getattr(result, "extracted_text", "") or "")
        if media:
            children += _text_section("Telegram 媒体元数据", "\n".join(
                f"{key}: {media[key]}" for key in _MEDIA_FIELDS if media.get(key) is not None))

        attachment_block = None
        if attachment:
            try:
                attachment_block = await _upload_attachment(client, attachment)
            except (NotionError, KeyError, ValueError) as exc:
                log.warning("Notion 原始媒体上传失败：%s", type(exc).__name__)

        fallback_attachment = {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": rich_text("原始附件未上传到 Notion，请在对应的 Telegram 消息中查看。")}}
        if attachment_block:
            children.append(attachment_block)
        elif input_type in ("Image", "Voice", "PDF"):
            children.append(fallback_attachment)

        body = {"parent": {"type": "data_source_id", "data_source_id": ds_id},
                "properties": properties, "children": children}
        confirm_created = find_page if page_filter else None
        try:
            page = await _request(client, "POST", "/pages", confirm_created=confirm_created, json=body)
        except NotionError as exc:
            # 400 明确拒绝了请求。旧字段缺少选项权限或附件被拒时，仍保留正文。
            if exc.code != 400:
                raise
            detail = str(exc).lower()
            recoverable = False
            if attachment_block and not any(field in detail for field in ("property", "properties", "option")):
                children[-1] = fallback_attachment
                recoverable = True
            if not any(field in detail for field in ("file_upload", "children")):
                for key in ("InputType", "FetchTier"):
                    if properties.pop(key, None):
                        recoverable = True
                if status == "AI_PENDING" and properties.get("Status", {}).get("select", {}).get("name") == "AI_PENDING":
                    options = {item.get("name") for item in schema["Status"]["select"].get("options", [])}
                    if "FALLBACK" in options:
                        properties["Status"] = {"select": {"name": "FALLBACK"}}
                    else:
                        properties.pop("Status")
                    recoverable = True
            if not recoverable:
                raise
            _schema_cache.pop(ds_id, None)
            page = await _request(client, "POST", "/pages", confirm_created=confirm_created, json=body)
        return page["url"], False
