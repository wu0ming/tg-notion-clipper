"""
Description: Notion 写入器（归档总表：URL 幂等去重 + 长文本自动切片），直接调用官方 REST API
Changelog:
- v1.0 (2026-11): 方案原始版本（notion-client databases.query，按分类分库）
- v1.1 (2026-11): httpx 直连 REST API，固定 Notion-Version 2025-09-03，自动由 database_id 解析 data_source_id
- v1.2 (2026-11): 改为单库：所有内容写入「归档总表」（NOTION_DB_ARCHIVE），Category 下拉框是唯一分类依据；
  去重范围随之扩大到全部分类
- v2.0 (2026-11): 修复缺少 import asyncio 的 bug（429/5xx 重试时 asyncio.sleep 会报 NameError）；
  改为模块级 httpx.AsyncClient 连接池，减少重复 TLS 握手
"""
import asyncio
from datetime import datetime, timezone

import httpx

from config import env
from utils import build_properties, build_children

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
_ds_cache: dict[str, str] = {}
_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


class NotionError(RuntimeError):
    pass


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
    return {"Authorization": f"Bearer {env('NOTION_TOKEN')}", "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json"}


def _clean_id(raw: str) -> str:
    raw = raw.strip().split("?")[0].rstrip("/").split("/")[-1]
    return raw.split("-")[-1] if len(raw) > 32 and "-" in raw and len(raw.split("-")[-1]) == 32 else raw


async def _request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> dict:
    """带重试：网络断开 / 429 限流 / 5xx 时最多重试 3 次（1s、2s、4s，429 优先按 Retry-After 等待）"""
    last = None
    for attempt in range(4):
        try:
            resp = await client.request(method, f"{NOTION_API}{path}", headers=_headers(), timeout=30.0, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last = NotionError(f"Notion 网络错误：{type(exc).__name__} {exc}")
            await asyncio.sleep(2 ** attempt)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            last = NotionError(f"Notion {resp.status_code}：{resp.text[:200]}")
            wait = float(resp.headers.get("retry-after", 2 ** attempt) or 1)
            await asyncio.sleep(min(wait, 10))
            continue
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message", resp.text)
            except Exception:
                detail = resp.text
            raise NotionError(f"Notion {resp.status_code}：{detail[:300]}")
        return resp.json()
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
        if "404" in str(exc) or "400" in str(exc):
            await _request(client, "GET", f"/data_sources/{key}")  # 本身就是 data source ID
            ds_id = key
        else:
            raise
    _ds_cache[key] = ds_id
    return ds_id


async def find_existing(url: str) -> str | None:
    """归档总表里已有同一归一化链接时返回页面链接，否则 None（供入口在抓取/调用 Gemini 前先查重，省钱省时）"""
    db_id = env("NOTION_DB_ARCHIVE")
    if not url or not db_id:
        return None
    client = _get_client()
    ds_id = await resolve_data_source_id(client, db_id)
    found = await _request(client, "POST", f"/data_sources/{ds_id}/query",
                           json={"filter": {"property": "URL", "url": {"equals": url}}, "page_size": 1})
    res = found.get("results") or []
    return res[0]["url"] if res else None


async def save_to_notion(result, source_type: str, url: str, status: str, raw_content: str,
                         user_note: str = "", fetch_note: str = "") -> tuple[str, bool]:
    """写入归档总表，返回 (Notion 页面链接, 是否重复)"""
    db_id = env("NOTION_DB_ARCHIVE")
    if not db_id:
        raise NotionError("未配置 NOTION_DB_ARCHIVE（归档总表 ID），请先运行 python setup_notion.py")

    client = _get_client()
    ds_id = await resolve_data_source_id(client, db_id)

    # 1. URL 幂等查重（同一归一化链接只建一页，跨所有分类）
    if url:
        found = await _request(client, "POST", f"/data_sources/{ds_id}/query",
                               json={"filter": {"property": "URL", "url": {"equals": url}}, "page_size": 1})
        if found.get("results"):
            return found["results"][0]["url"], True

    # 2. 属性（Category 写入新名称时 Notion 会自动新增下拉选项）+ 3. 正文块
    properties = build_properties(result.title, result.category, result.tags, source_type, url,
                                  result.summary, status, datetime.now(timezone.utc).isoformat())
    children = build_children(result.key_points, raw_content[:36000], user_note, url, status, fetch_note)
    page = await _request(client, "POST", "/pages", json={
        "parent": {"type": "data_source_id", "data_source_id": ds_id},
        "properties": properties, "children": children})
    return page["url"], False
