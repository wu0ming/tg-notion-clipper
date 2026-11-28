"""
Description: Notion 归档、收件箱、多附件、增量合并与原始媒体留存，直接调用官方 REST API
Changelog:
- v1.0 (2026-11): 方案原始版本（notion-client databases.query，按分类分库）
- v1.1 (2026-11): httpx 直连 REST API，固定 Notion-Version 2025-09-03，自动由 database_id 解析 data_source_id
- v1.2 (2026-11): 改为单库：所有内容写入「归档总表」（NOTION_DB_ARCHIVE），Category 下拉框是唯一分类依据；
  去重范围随之扩大到全部分类
- v2.0 (2026-11): 修复缺少 import asyncio 的 bug（429/5xx 重试时 asyncio.sleep 会报 NameError）；
  改为模块级 httpx.AsyncClient 连接池，减少重复 TLS 握手
- v2.4 (2026-11): 支持多用户独立库隔离（scope）、多附件/视频块上传、100-block 分片、2.5 req/s 限流、
  ProjectKey/AlbumKey 跨来源增量合并与实体模板渲染
"""
import asyncio
import hashlib
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx

from config import env
from template_config import (
    build_template_scalars,
    get_template,
    paragraph_chunks,
    render_template_blocks,
)
from utils import build_children, build_entity_sections, build_properties, chunk_text, clean_tags, rich_text

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
MAX_CHILDREN_PER_BATCH = 100
NOTION_MIN_INTERVAL = 0.4  # <= 2.5 req/s

_ds_cache: dict[str, str] = {}
_schema_cache: dict[str, tuple[float, dict]] = {}
_write_locks: dict[tuple[asyncio.AbstractEventLoop, str], tuple[asyncio.Lock, int]] = {}
_rate_locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
_last_req_at: dict[asyncio.AbstractEventLoop, float] = {}
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
                 "file_name", "file_size", "size", "duration", "duration_seconds", "width", "height",
                 "date", "media_group_id", "telegram_url", "archive_note")


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


def _headers(token: str = "") -> dict:
    return {"Authorization": f"Bearer {token or env('NOTION_TOKEN')}", "Notion-Version": NOTION_VERSION}


def _clean_id(raw: str) -> str:
    raw = raw.strip().split("?")[0].rstrip("/").split("/")[-1]
    return raw.split("-")[-1] if len(raw) > 32 and "-" in raw and len(raw.split("-")[-1]) == 32 else raw


def _token_scope(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12] if token else ""


def _cache_key(raw_id: str, token: str = "") -> str:
    cleaned = _clean_id(raw_id)
    return f"{_token_scope(token)}:{cleaned}" if token else cleaned


async def _pace_request(client: httpx.AsyncClient) -> None:
    if not isinstance(getattr(client, "_transport", None), httpx.AsyncHTTPTransport):
        return
    loop = asyncio.get_running_loop()
    lock = _rate_locks.setdefault(loop, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        wait = NOTION_MIN_INTERVAL - (now - _last_req_at.get(loop, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_req_at[loop] = time.monotonic()


async def _request(client: httpx.AsyncClient, method: str, path: str, *, confirm_created=None,
                   token: str = "", **kwargs) -> dict:
    """查询可重试。创建页面结果不明时查重确认，避免再次提交时创建副本。"""
    last = None
    for attempt in range(4):
        wait = 2 ** attempt
        uncertain = False
        try:
            await _pace_request(client)
            resp = await client.request(method, f"{NOTION_API}{path}", headers=_headers(token), timeout=30.0, **kwargs)
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


async def resolve_data_source_id(client: httpx.AsyncClient, database_or_ds_id: str, *, token: str = "") -> str:
    """环境变量里填数据库 ID（浏览器地址栏那 32 位）或 data source ID 均可"""
    key = _cache_key(database_or_ds_id, token)
    raw_id = _clean_id(database_or_ds_id)
    if key in _ds_cache:
        return _ds_cache[key]
    try:
        db = await _request(client, "GET", f"/databases/{raw_id}", token=token)
        sources = db.get("data_sources") or []
        if not sources:
            raise NotionError("该数据库下没有 data source")
        ds_id = sources[0]["id"]
    except NotionError as exc:
        if exc.code in (400, 404):
            schema = await _request(client, "GET", f"/data_sources/{raw_id}", token=token)
            _schema_cache[_cache_key(raw_id, token)] = (time.monotonic(), schema.get("properties", {}))
            ds_id = raw_id
        else:
            raise
    _ds_cache[key] = ds_id
    return ds_id


async def _data_source_schema(client: httpx.AsyncClient, ds_id: str, *, token: str = "") -> dict:
    key = _cache_key(ds_id, token)
    cached_at, schema = _schema_cache.get(key, (0, {}))
    if time.monotonic() - cached_at >= 300 or key not in _schema_cache:
        data = await _request(client, "GET", f"/data_sources/{ds_id}", token=token)
        schema = data.get("properties", {})
        _schema_cache[key] = (time.monotonic(), schema)
    return schema


def _destination(inbox: bool, scope: dict | None = None) -> str:
    if scope is not None:
        return (scope.get("inbox_db") if inbox else "") or scope.get("archive_db") or ""
    return (env("NOTION_DB_INBOX") if inbox else "") or env("NOTION_DB_ARCHIVE")


def _scope_token(scope: dict | None) -> str:
    return (scope or {}).get("notion_token", "")


def _lookup_filter(schema: dict, url: str, telegram_key: str = "", clip_key: str = "",
                   project_key: str = "", album_key: str = "") -> dict | None:
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
    if project_key and schema.get("ProjectKey", {}).get("type") == "rich_text":
        filters.append({"property": "ProjectKey", "rich_text": {"equals": project_key}})
    if album_key and schema.get("AlbumKey", {}).get("type") == "rich_text":
        filters.append({"property": "AlbumKey", "rich_text": {"equals": album_key}})
    if len(filters) > 1:
        return {"or": filters}
    return next(iter(filters), None)


async def _lookup_page(client: httpx.AsyncClient, ds_id: str, page_filter: dict | None, *, token: str = "") -> dict | None:
    if page_filter:
        found = await _request(client, "POST", f"/data_sources/{ds_id}/query", token=token,
                               json={"filter": page_filter, "page_size": 1})
        return next(iter(found.get("results") or []), None)
    return None


async def find_existing(url: str, *, inbox: bool = False, clip_key: str = "", project_key: str = "",
                        album_key: str = "", scope: dict | None = None) -> str | None:
    """按归一化 URL 或项目主键在指定归档库查重。未配置独立收件箱时沿用归档总表。"""
    db_id = _destination(inbox, scope)
    if (not url and not clip_key and not project_key and not album_key) or not db_id:
        return None
    token = _scope_token(scope)
    client = _get_client()
    ds_id = await resolve_data_source_id(client, db_id, token=token)
    schema = await _data_source_schema(client, ds_id, token=token)
    page = await _lookup_page(client, ds_id,
                              _lookup_filter(schema, url, clip_key=clip_key, project_key=project_key, album_key=album_key),
                              token=token)
    return page["url"] if page else None


async def find_existing_page(url: str = "", *, inbox: bool = False, project_key: str = "",
                             telegram_key: str = "", album_key: str = "", scope: dict | None = None) -> dict | None:
    """返回完整页面对象，支持跨来源增量合并与标签读取。"""
    token = _scope_token(scope)
    client = _get_client()
    databases = dict.fromkeys(_clean_id(db) for db in (_destination(inbox, scope), _destination(not inbox, scope)) if db)
    for db in databases:
        ds_id = await resolve_data_source_id(client, db, token=token)
        schema = await _data_source_schema(client, ds_id, token=token)
        if filt := _lookup_filter(schema, url, telegram_key=telegram_key, project_key=project_key, album_key=album_key):
            if page := await _lookup_page(client, ds_id, filt, token=token):
                return page
    return None


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


async def ensure_markdown_child(parent_url: str, markdown: str, *, scope: dict | None = None) -> None:
    """Create the Markdown as a native child page and repair partial browser captures on retry."""
    parent_id = _clean_id(parent_url)
    token = _scope_token(scope)
    client = _get_client()

    async def find_child() -> dict | None:
        cursor = None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = await _request(client, "GET", f"/blocks/{parent_id}/children", token=token, params=params)
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
            await _request(client, "POST", "/pages", confirm_created=find_child, token=token, json=body)
        except NotionError as exc:
            if exc.code != 400 or "Images must be standalone blocks" not in str(exc):
                raise
            separated = _split_markdown_images(markdown)
            if separated == markdown:
                raise
            body["markdown"] = separated
            await _request(client, "POST", "/pages", confirm_created=find_child, token=token, json=body)


def _telegram_key(metadata: dict) -> str:
    chat_id, message_id = metadata.get("chat_id"), metadata.get("message_id")
    if isinstance(chat_id, int) and isinstance(message_id, int):
        suffix = f":u{metadata['unit_index']}" if metadata.get("unit_index") is not None else ""
        return f"{chat_id}:{message_id}{suffix}"
    return ""


async def _append_blocks_batched(client: httpx.AsyncClient, page_id: str, blocks: list[dict], *,
                                 confirm_created=None, token: str = "") -> None:
    for start in range(0, len(blocks), MAX_CHILDREN_PER_BATCH):
        chunk = blocks[start:start + MAX_CHILDREN_PER_BATCH]
        await _request(client, "PATCH", f"/blocks/{page_id}/children",
                       confirm_created=confirm_created if start == 0 else None,
                       token=token, json={"children": chunk})


async def _list_blocks(client: httpx.AsyncClient, page_id: str, *, token: str = "") -> list[dict]:
    blocks = []
    params: dict = {"page_size": 100}
    while True:
        data = await _request(client, "GET", f"/blocks/{page_id}/children", token=token, params=params)
        blocks.extend(data.get("results") or [])
        cursor = data.get("next_cursor")
        if not cursor or not data.get("has_more"):
            break
        params["start_cursor"] = cursor
    return blocks


def _block_heading_text(block: dict) -> str:
    btype = block.get("type", "")
    if btype not in {"heading_1", "heading_2", "heading_3"}:
        return ""
    parts = block[btype].get("rich_text", [])
    return "".join(part.get("plain_text") or part.get("text", {}).get("content", "") for part in parts)


async def append_telegram_note(chat_id: int, target_id: int, message_id: int, note: str,
                               *, scope: dict | None = None) -> str | None:
    """Use the archived message mapping and a stable marker to avoid duplicating a reply."""
    token = _scope_token(scope)
    client = _get_client()
    telegram_key = _telegram_key({"chat_id": chat_id, "message_id": target_id})
    page = None
    databases = dict.fromkeys(_clean_id(db) for db in (_destination(False, scope), _destination(True, scope)) if db)
    for db in databases:
        ds_id = await resolve_data_source_id(client, db, token=token)
        schema = await _data_source_schema(client, ds_id, token=token)
        page = await _lookup_page(client, ds_id, _lookup_filter(schema, "", telegram_key), token=token)
        if page:
            break
    if not page:
        return None

    path = f"/blocks/{page['id']}/children"
    title = f"个人批注 · Telegram #{message_id}"

    async def find_note() -> dict | None:
        params = {"page_size": 100}
        while True:
            data = await _request(client, "GET", path, token=token, params=params)
            for block in data.get("results", []):
                if block.get("type") == "heading_2" and _block_heading_text(block) == title:
                    return block
            cursor = data.get("next_cursor")
            if not cursor:
                return None
            params["start_cursor"] = cursor

    async with _serialized_write(f"telegram-note:{page['id']}:{message_id}"):
        if not await find_note():
            await _request(client, "PATCH", path, confirm_created=find_note, token=token,
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


def _visible_note_blocks(title: str, content: str, emoji: str = "💬") -> list[dict]:
    """Render user-provided caption/note visibly at the top instead of hiding inside a collapsed toggle."""
    pieces = chunk_text(content, 1800)[:24]
    if not pieces:
        return []
    blocks = [{"object": "block", "type": "callout", "callout": {
        "rich_text": rich_text(f"{title}：{pieces[0]}"),
        "icon": {"type": "emoji", "emoji": emoji},
    }}]
    if len(pieces) > 1:
        blocks += _text_section(f"{title}（续）", "\n\n".join(pieces[1:]))
    return blocks


def _forwarded_text_section(title: str, content: str) -> list[dict]:
    pieces = chunk_text(content, 1800)[:24]
    if not pieces:
        return []
    if len(pieces) == 1 and len((content or "").strip()) <= 1200:
        return [
            {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich_text(title)}},
            {"object": "block", "type": "quote", "quote": {"rich_text": rich_text(pieces[0])}},
        ]
    return _text_section(title, content)


async def upload_file(client: httpx.AsyncClient, attachment: tuple[bytes, str, str], *, token: str = "") -> str:
    content, mime_type, filename = attachment
    filename = re.sub(r"[\x00-\x1f]", "", filename.replace("\\", "/").split("/")[-1]).strip() or "telegram-media"
    # Notion 文件名限制按字节计算。保留扩展名供 MIME 类型校验。
    if len(filename.encode("utf-8")) > 900:
        stem, dot, ext = filename.rpartition(".")
        filename = (stem or filename).encode("utf-8")[:800].decode("utf-8", "ignore") + (dot + ext[:20] if stem else "")
    upload = await _request(client, "POST", "/file_uploads", token=token, json={})
    uploaded = await _request(client, "POST", f"/file_uploads/{upload['id']}/send", token=token,
                              files={"file": (filename, content, mime_type)})
    if uploaded.get("status") != "uploaded":
        raise NotionError("Notion 未确认原始媒体上传")
    return upload["id"]


def _media_block(upload_id: str, mime_type: str, caption: str = "Telegram 原始附件") -> dict:
    if mime_type.startswith("image/"):
        btype = "image"
    elif mime_type.startswith("video/"):
        btype = "video"
    else:
        btype = "file"
    return {"object": "block", "type": btype, btype: {
        "type": "file_upload", "file_upload": {"id": upload_id}, "caption": rich_text(caption)}}


async def _upload_attachment(client: httpx.AsyncClient, attachment: tuple[bytes, str, str], *,
                             token: str = "", caption: str = "Telegram 原始附件") -> dict:
    upload_id = await upload_file(client, attachment, token=token)
    return _media_block(upload_id, attachment[1], caption=caption)


async def save_to_notion(result, source_type: str, url: str, status: str, raw_content: str,
                         user_note: str = "", fetch_note: str = "", *, inbox: bool = False,
                         input_type: str = "", fetch_tier: str = "", original_url: str = "",
                         media_metadata: dict | list[dict] | None = None,
                         attachment: tuple[bytes, str, str] | None = None,
                         attachments: list[tuple[bytes, str, str]] | None = None,
                         clip_key: str = "", telegram_message: dict | None = None,
                         forwarded_text: str = "", forward_metadata: dict | None = None,
                         accompanying_text: str = "", deduplicate_by_url: bool = True,
                         reference_links: tuple[str, ...] = (),
                         project_key: str = "", album_key: str = "",
                         entity_type: str = "", entity_details: dict | list | None = None,
                         extra_blocks: list[dict] | None = None,
                         scope: dict | None = None) -> tuple[str, bool]:
    """写入归档总表或收件箱，返回 (Notion 页面链接, 是否重复)。"""
    db_id = _destination(inbox, scope)
    if not db_id:
        raise NotionError("未配置 NOTION_DB_ARCHIVE（归档总表 ID），请先运行 python setup_notion.py")

    token = _scope_token(scope)
    client = _get_client()
    ds_id = await resolve_data_source_id(client, db_id, token=token)
    schema = await _data_source_schema(client, ds_id, token=token)
    first_media = media_metadata[0] if isinstance(media_metadata, list) and media_metadata else (
        media_metadata if isinstance(media_metadata, dict) else {}
    )
    telegram_key = _telegram_key(telegram_message or first_media)
    lookup_url = url if deduplicate_by_url else ""
    page_filter = _lookup_filter(schema, lookup_url, telegram_key, clip_key, project_key=project_key if deduplicate_by_url else "")

    async def find_page() -> dict | None:
        return await _lookup_page(client, ds_id, page_filter, token=token)

    tenant_prefix = f"t{scope.get('tenant_id', 0)}:" if scope else ""
    async with _serialized_write(f"{tenant_prefix}{clip_key or lookup_url or telegram_key}"):
        if existing := await find_page():
            return existing["url"], True
        other_db = _destination(not inbox, scope)
        if other_db and _clean_id(other_db) != _clean_id(db_id) and (lookup_url or telegram_key):
            try:
                other_ds = await resolve_data_source_id(client, other_db, token=token)
                other_schema = await _data_source_schema(client, other_ds, token=token)
                other_filter = _lookup_filter(other_schema, lookup_url, telegram_key, clip_key,
                                              project_key=project_key if deduplicate_by_url else "")
                existing = await _lookup_page(client, other_ds, other_filter, token=token)
                if existing:
                    return existing["url"], True
            except NotionError as exc:
                log.warning("Notion 跨库查重失败（HTTP %s）", exc.code or "未收到响应")

        created_iso = datetime.now(timezone.utc).isoformat()
        properties = build_properties(result.title, result.category, result.tags, source_type, url,
                                      result.summary, status, created_iso)
        if input_type:
            properties["InputType"] = {"select": {"name": input_type}}
        if fetch_tier:
            properties["FetchTier"] = {"select": {"name": fetch_tier}}
        resolved_entity = entity_type or getattr(result, "entity_type", "")
        if resolved_entity:
            properties["EntityType"] = {"select": {"name": resolved_entity}}
        if telegram_key:
            properties["TelegramKey"] = {"rich_text": rich_text(telegram_key)}
        if clip_key:
            properties["ClipKey"] = {"rich_text": rich_text(clip_key)}
        if project_key:
            properties["ProjectKey"] = {"rich_text": rich_text(project_key)}
        if album_key:
            properties["AlbumKey"] = {"rich_text": rich_text(album_key)}
        properties = _fit_properties(properties, schema)

        origin_label = ""
        if forward_metadata:
            origin_label = f"Telegram 转发（{forward_metadata.get('来源类型') or '未知'}）"
        elif telegram_message is not None and (telegram_message.get("chat_id") is not None or telegram_message.get("message_id") is not None):
            origin_label = "本人直发（含个人批注）" if (user_note and not accompanying_text) else "本人直发"
        channel_details = " · ".join(filter(None, (origin_label, input_type, fetch_tier)))
        resolved_details = entity_details if entity_details is not None else getattr(result, "entity_details", None)
        entity_blocks = build_entity_sections(resolved_entity, resolved_details)
        media_list = media_metadata if isinstance(media_metadata, list) else ([media_metadata] if media_metadata else [])

        all_attachments = list(attachments or [])
        if attachment and not all_attachments:
            all_attachments = [attachment]

        fallback_attachment = {"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": rich_text("原始附件未上传到 Notion，请在对应的 Telegram 消息中查看。")}}
        attachment_blocks: list[dict] = []
        for idx, att in enumerate(all_attachments, 1):
            caption = "Telegram 原始附件" if len(all_attachments) == 1 else f"Telegram 原始附件 #{idx} ({att[2]})"
            try:
                attachment_blocks.append(await _upload_attachment(client, att, token=token, caption=caption))
            except (NotionError, KeyError, ValueError) as exc:
                log.warning("Notion 原始媒体上传失败：%s", type(exc).__name__)
                attachment_blocks.append({"object": "block", "type": "paragraph", "paragraph": {
                    "rich_text": rich_text(f"原始附件 {att[2]} 未上传到 Notion，请在对应的 Telegram 消息中查看。")}})
        if not all_attachments and input_type in ("Image", "Voice", "PDF", "Video"):
            attachment_blocks.append(fallback_attachment)

        tpl_arg = scope.get("template_page") if scope is not None else None
        template_blocks, _, _ = await get_template(template_page=tpl_arg, archive_db=db_id, notion_token=token)
        if template_blocks:
            scalars = build_template_scalars(
                result=result, source_type=source_type, url=url, status=status,
                user_note=user_note, accompanying_text=accompanying_text, fetch_note=fetch_note,
                input_type=input_type, fetch_tier=fetch_tier, origin_label=origin_label,
                channel_details=channel_details, original_url=original_url,
                created_iso=created_iso, project_key=project_key, entity_type=resolved_entity,
            )
            forward_src_blocks: list[dict] = []
            if forward_metadata:
                if meta_lines := "\n".join(f"{k}：{v}" for k, v in forward_metadata.items() if k != "链接"):
                    forward_src_blocks.append({"object": "block", "type": "paragraph",
                                               "paragraph": {"rich_text": rich_text(meta_lines)}})
                if forward_url := forward_metadata.get("链接"):
                    forward_src_blocks.append({"object": "block", "type": "bookmark", "bookmark": {"url": forward_url}})
            media_meta_blocks: list[dict] = []
            for idx, m_item in enumerate(media_list, 1):
                meta_text = "\n".join(f"{k}: {m_item[k]}" for k in _MEDIA_FIELDS if m_item.get(k) is not None)
                if meta_text:
                    prefix = f"[媒体 #{idx}]\n" if len(media_list) > 1 else ""
                    media_meta_blocks.append({"object": "block", "type": "paragraph",
                                              "paragraph": {"rich_text": rich_text(f"{prefix}{meta_text}")}})
            providers = {
                "key_points": [{"object": "block", "type": "bulleted_list_item",
                                "bulleted_list_item": {"rich_text": rich_text(pt)}} for pt in result.key_points[:12]],
                "raw_content": paragraph_chunks(raw_content[:36000], limit=1800, max_chunks=20),
                "extracted_text": paragraph_chunks(getattr(result, "extracted_text", "") or "", limit=1800, max_chunks=24),
                "entity_sections": entity_blocks,
                "video_steps": list(extra_blocks or []),
                "forwarded_section": _forwarded_text_section("Telegram 转发原文", forwarded_text),
                "forwarded_text": paragraph_chunks(forwarded_text, limit=1800, max_chunks=24),
                "forward_source": forward_src_blocks,
                "reference_links": [{"object": "block", "type": "bookmark", "bookmark": {"url": lnk[:2000]}}
                                    for lnk in reference_links],
                "media_metadata": media_meta_blocks,
                "attachments": attachment_blocks,
            }
            children, used_vars = render_template_blocks(template_blocks, scalars, providers)
            if scalars["user_note"] and "user_note" not in used_vars:
                children[0:0] = [{"object": "block", "type": "callout", "callout": {
                    "rich_text": rich_text(f"个人批注：{scalars['user_note']}"),
                    "icon": {"type": "emoji", "emoji": "📝"}, "color": "yellow_background",
                }}]
            if extra_blocks and "video_steps" not in used_vars:
                children.extend(extra_blocks)
            if (forwarded_text or forward_metadata) and not ({"forwarded_section", "forwarded_text", "forward_source"} & used_vars):
                children += _forwarded_text_section("Telegram 转发原文", forwarded_text)
                if forward_metadata:
                    children += _text_section("Telegram 转发来源", "\n".join(
                        f"{key}：{value}" for key, value in forward_metadata.items() if key != "链接"))
                    if forward_url := forward_metadata.get("链接"):
                        children.append({"object": "block", "type": "bookmark", "bookmark": {"url": forward_url}})
            if attachment_blocks and "attachments" not in used_vars:
                children.extend(attachment_blocks)
        else:
            children = build_children(result.key_points[:12], raw_content[:36000],
                                      "" if accompanying_text else user_note, url, status, fetch_note)
            if accompanying_blocks := _visible_note_blocks("随附说明（来源未确认）", accompanying_text, "💬"):
                insert_at = 0
                while insert_at < len(children) and children[insert_at].get("type") == "callout":
                    insert_at += 1
                children[insert_at:insert_at] = accompanying_blocks
            children += entity_blocks
            if extra_blocks:
                children.extend(extra_blocks)
            children += _forwarded_text_section("Telegram 转发原文", forwarded_text)
            if forward_metadata:
                children += _text_section("Telegram 转发来源", "\n".join(
                    f"{key}：{value}" for key, value in forward_metadata.items() if key != "链接"))
                if forward_url := forward_metadata.get("链接"):
                    children.append({"object": "block", "type": "bookmark", "bookmark": {"url": forward_url}})
            if result.summary and "Summary" not in properties:
                children += _text_section("摘要", result.summary)
            if channel_details:
                children += [{"object": "block", "type": "paragraph", "paragraph": {
                    "rich_text": rich_text(f"输入与归档渠道：{channel_details}")}}]
            if original_url and (original_url != url or len(original_url) > 2000):
                children += _text_section("原始链接", original_url)
            if fetch_note:
                children += _text_section("处理记录", fetch_note)
            if reference_links:
                children.append({"object": "block", "type": "heading_2", "heading_2": {
                    "rich_text": rich_text("评论原文链接")}})
                children.extend({"object": "block", "type": "bookmark", "bookmark": {"url": link[:2000]}}
                                for link in reference_links)
            children += _text_section("提取文字 / 语音转写", getattr(result, "extracted_text", "") or "")

            for idx, m_item in enumerate(media_list, 1):
                label = "Telegram 媒体元数据" if len(media_list) == 1 else f"Telegram 媒体元数据 #{idx}"
                children += _text_section(label, "\n".join(
                    f"{key}: {m_item[key]}" for key in _MEDIA_FIELDS if m_item.get(key) is not None))
            children.extend(attachment_blocks)

        uploaded_indices: list[int] = [
            i for i, blk in enumerate(children)
            if blk.get("type") in {"image", "video", "file"}
            and (blk.get(blk["type"], {}).get("type") == "file_upload")
        ]

        initial_children = children[:MAX_CHILDREN_PER_BATCH]
        trailing_children = children[MAX_CHILDREN_PER_BATCH:]
        body = {"parent": {"type": "data_source_id", "data_source_id": ds_id},
                "properties": properties, "children": initial_children}
        confirm_created = find_page if page_filter else None
        try:
            page = await _request(client, "POST", "/pages", confirm_created=confirm_created, token=token, json=body)
        except NotionError as exc:
            if exc.code != 400:
                raise
            detail = str(exc).lower()
            recoverable = False
            if uploaded_indices and not any(field in detail for field in ("property", "properties", "option")):
                for pos in uploaded_indices:
                    if pos < len(initial_children):
                        initial_children[pos] = fallback_attachment
                recoverable = True
            if not any(field in detail for field in ("file_upload", "children")):
                for key in ("InputType", "FetchTier", "EntityType", "ProjectKey", "AlbumKey"):
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
            _schema_cache.pop(_cache_key(ds_id, token), None)
            page = await _request(client, "POST", "/pages", confirm_created=confirm_created, token=token, json=body)
        if trailing_children:
            await _append_blocks_batched(client, page["id"], trailing_children, token=token)
        return page["url"], False


async def enrich_notion_page(page_id_or_url: str, *, source_type: str, url: str = "", raw_content: str = "",
                             user_note: str = "", accompanying_text: str = "", forwarded_text: str = "",
                             forward_metadata: dict | None = None, key_points: list[str] | None = None,
                             attachments: list[tuple[bytes, str, str]] | None = None,
                             tags: list[str] | None = None, source_key: str = "",
                             input_type_override: str = "",
                             scope: dict | None = None) -> tuple[str, bool]:
    """跨来源/晚到相册成员增量合并：保留已有分类和人工编辑，不重复追加同一来源。"""
    token = _scope_token(scope)
    client = _get_client()
    page_id = _clean_id(page_id_or_url)
    page = await _request(client, "GET", f"/pages/{page_id}", token=token)
    marker = f"补充来源 · {source_type}" + (f" · {source_key}" if source_key else "")

    async def find_marker() -> dict | None:
        for block in await _list_blocks(client, page_id, token=token):
            if _block_heading_text(block) == marker:
                return block
        return None

    async with _serialized_write(f"enrich:{page_id}:{source_key or source_type}"):
        if source_key and await find_marker():
            return page["url"], True
        blocks: list[dict] = [{"object": "block", "type": "heading_2", "heading_2": {
            "rich_text": rich_text(marker)}}]
        if url:
            blocks.append({"object": "block", "type": "bookmark", "bookmark": {"url": url[:2000]}})
        if user_note:
            blocks.append({"object": "block", "type": "callout", "callout": {
                "rich_text": rich_text(f"补充批注：{user_note}"), "icon": {"type": "emoji", "emoji": "📝"}}})
        if accompanying_text:
            blocks += _visible_note_blocks("随附说明（来源未确认）", accompanying_text, "💬")
        if forwarded_text:
            blocks += _forwarded_text_section("补充转发原文", forwarded_text)
        if forward_metadata:
            blocks += _text_section("补充转发来源", "\n".join(
                f"{k}：{v}" for k, v in forward_metadata.items() if k != "链接"))
        for point in (key_points or [])[:8]:
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": rich_text(point)}})
        if raw_content:
            blocks += _text_section("补充来源正文", raw_content[:24000])
        for idx, att in enumerate(attachments or [], 1):
            try:
                blocks.append(await _upload_attachment(client, att, token=token, caption=f"补充附件 #{idx} ({att[2]})"))
            except (NotionError, KeyError, ValueError) as exc:
                log.warning("补充附件上传失败：%s", type(exc).__name__)
                blocks.append({"object": "block", "type": "paragraph", "paragraph": {
                    "rich_text": rich_text(f"补充附件 {att[2]} 未上传到 Notion，请在 Telegram 消息中查看。")}})

        await _append_blocks_batched(client, page_id, blocks, confirm_created=find_marker, token=token)

        prop_updates: dict = {}
        if tags:
            existing_tags = [item["name"] for item in ((page.get("properties") or {}).get("Tags") or {}).get("multi_select", []) if item.get("name")]
            merged = clean_tags([*existing_tags, *tags], limit=8)
            if merged != existing_tags and "Tags" in (page.get("properties") or {}):
                prop_updates["Tags"] = {"multi_select": [{"name": t} for t in merged]}
        if input_type_override and "InputType" in (page.get("properties") or {}):
            prop_updates["InputType"] = {"select": {"name": input_type_override}}
        if prop_updates:
            try:
                await _request(client, "PATCH", f"/pages/{page_id}", token=token, json={"properties": prop_updates})
            except NotionError as exc:
                log.warning("更新合并属性失败：%s", exc.code or type(exc).__name__)
        return page["url"], False


async def update_page_tags(page_id_or_url: str, tags: list[str], *, scope: dict | None = None) -> list[str]:
    token = _scope_token(scope)
    client = _get_client()
    page_id = _clean_id(page_id_or_url)
    cleaned = clean_tags(tags, limit=8)
    await _request(client, "PATCH", f"/pages/{page_id}", token=token,
                   json={"properties": {"Tags": {"multi_select": [{"name": t} for t in cleaned]}}})
    return cleaned


async def fetch_page_content(page_id_or_url: str, *, scope: dict | None = None) -> dict:
    token = _scope_token(scope)
    client = _get_client()
    page_id = _clean_id(page_id_or_url)
    page = await _request(client, "GET", f"/pages/{page_id}", token=token)
    blocks = await _list_blocks(client, page_id, token=token)
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        body = block.get(btype) or {}
        if "rich_text" in body:
            text = "".join(p.get("plain_text") or p.get("text", {}).get("content", "") for p in body["rich_text"])
            if text:
                lines.append(text)
    return {"page_id": page_id, "url": page.get("url", ""), "properties": page.get("properties", {}), "text": "\n".join(lines)}


def _extract_page_summary(page: dict) -> dict:
    props = page.get("properties") or {}
    title = ""
    for prop in props.values():
        if prop.get("type") == "title":
            title = "".join(p.get("plain_text") or p.get("text", {}).get("content", "") for p in prop.get("title") or [])
            break
    pid = _clean_id(page.get("id") or page.get("url") or "")
    category = ((props.get("Category") or {}).get("select") or {}).get("name") or "待整理"
    tags = [item["name"] for item in (props.get("Tags") or {}).get("multi_select") or [] if item.get("name")]
    status_prop = props.get("Status") or {}
    status = ((status_prop.get("select") or status_prop.get("status") or {}).get("name")) or "PASS"
    return {
        "id": pid,
        "page_id": pid,
        "page_url": page.get("url", ""),
        "title": title or "未命名归档",
        "category": category,
        "tags": tags,
        "canonical_url": (props.get("URL") or {}).get("url") or "",
        "status": status,
    }


async def get_page_summary(page_id_or_url: str, *, scope: dict | None = None) -> dict | None:
    """Stateless Cloud Run fallback: fetch page metadata directly from Notion when local SQLite is cold."""
    pid = _clean_id(page_id_or_url)
    if not pid:
        return None
    token = _scope_token(scope)
    try:
        page = await _request(_get_client(), "GET", f"/pages/{pid}", token=token)
    except NotionError:
        return None
    return _extract_page_summary(page)


async def query_notion_clips(query: str = "", *, limit: int = 5, inbox: bool = False,
                             scope: dict | None = None) -> list[dict]:
    """Stateless Cloud Run fallback for /find and /recent against Notion when local SQLite is empty."""
    db_id = _destination(inbox, scope)
    if not db_id:
        return []
    token = _scope_token(scope)
    client = _get_client()
    ds_id = await resolve_data_source_id(client, db_id, token=token)
    schema = await _data_source_schema(client, ds_id, token=token)
    payload: dict = {"page_size": max(1, min(20, limit))}
    q = (query or "").strip()
    if q:
        clauses = []
        for name, prop in schema.items():
            ptype = prop.get("type")
            if ptype == "title":
                clauses.append({"property": name, "title": {"contains": q}})
            elif name == "Summary" and ptype == "rich_text":
                clauses.append({"property": name, "rich_text": {"contains": q}})
        if clauses:
            payload["filter"] = {"or": clauses} if len(clauses) > 1 else clauses[0]
    if "Created" in schema:
        payload["sorts"] = [{"property": "Created", "direction": "descending"}]
    else:
        payload["sorts"] = [{"timestamp": "created_time", "direction": "descending"}]
    found = await _request(client, "POST", f"/data_sources/{ds_id}/query", token=token, json=payload)
    return [_extract_page_summary(page) for page in (found.get("results") or [])]
