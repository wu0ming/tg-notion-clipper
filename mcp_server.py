"""MCP server exposing tenant-isolated clip retrieval and note appending (#12)."""
from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from clip_store import ClipStore, get_store
from config import env, resolve_user_context
import notion_writer

TOOLS_SPEC = [
    {
        "name": "search_clips",
        "description": "使用本地 FTS5 关键词检索用户已归档的知识条目（返回标题、标签、分类与 Notion 链接）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "theme": {"type": "string", "default": ""},
                "tags": {"type": "array", "items": {"type": "string"}},
                "user_id": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_clip_content",
        "description": "按 page_id 或 clip_id 读取已归档条目的完整内容（需通过租户权限校验）。",
        "inputSchema": {
            "type": "object",
            "properties": {"page_id": {"type": "string"}, "user_id": {"type": "integer"}},
            "required": ["page_id"],
        },
    },
    {
        "name": "list_recent_clips",
        "description": "列出最近 N 天内已归档的知识条目。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 7},
                "limit": {"type": "integer", "default": 20},
                "user_id": {"type": "integer"},
            },
        },
    },
    {
        "name": "append_clip_note",
        "description": "向指定归档页面追加个人批注并同步本地索引（需通过租户权限校验）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page_id": {"type": "string"},
                "note": {"type": "string"},
                "user_id": {"type": "integer"},
            },
            "required": ["page_id", "note"],
        },
    },
]


def _resolve_tenant(user_id: int | None):
    raw_uid = user_id
    if raw_uid is None and (default_uid := env("CLIPPER_MCP_USER_ID")):
        if default_uid.lstrip("-").isdigit():
            raw_uid = int(default_uid)
    ctx = resolve_user_context(raw_uid)
    if ctx is None:
        raise PermissionError("未授权或未配置知识库的用户")
    return ctx


async def search_clips(query: str, theme: str = "", tags: list[str] | None = None,
                       user_id: int | None = None, *, store: ClipStore | None = None) -> list[dict]:
    ctx = _resolve_tenant(user_id)
    st = store or get_store()
    rows = st.search_clips(query, tenant_id=ctx.user_id, theme=theme, tags=tags, limit=10)
    return [{
        "id": r["id"], "page_id": r["page_id"], "title": r["title"],
        "category": r["category"], "entity_type": r["entity_type"], "tags": r["tags"],
        "url": r["canonical_url"], "notion_url": r["page_url"], "summary": r["summary"],
    } for r in rows]


async def get_clip_content(page_id: str, user_id: int | None = None, *, store: ClipStore | None = None) -> dict:
    ctx = _resolve_tenant(user_id)
    st = store or get_store()
    clip = st.get_clip(page_id, tenant_id=ctx.user_id)
    if not clip:
        raise PermissionError("未找到该页面或无权访问")
    full_text = clip.get("content_preview", "")
    if ctx.notion_token and ctx.notion_token != "x":
        try:
            remote = await notion_writer.fetch_page_content(clip["page_id"], scope=ctx.notion_scope())
            if remote.get("text"):
                full_text = remote["text"]
        except Exception:
            pass
    return {
        "page_id": clip["page_id"],
        "notion_url": clip["page_url"],
        "title": clip["title"],
        "category": clip["category"],
        "tags": clip["tags"],
        "url": clip["canonical_url"],
        "summary": clip["summary"],
        "user_note": clip["user_note"],
        "content": full_text,
    }


async def list_recent_clips(days: int = 7, limit: int = 20, user_id: int | None = None,
                            *, store: ClipStore | None = None) -> list[dict]:
    ctx = _resolve_tenant(user_id)
    st = store or get_store()
    rows = st.list_recent_clips(tenant_id=ctx.user_id, days=max(1, days), limit=max(1, min(50, limit)))
    return [{
        "page_id": r["page_id"], "title": r["title"], "category": r["category"],
        "tags": r["tags"], "status": r["status"], "notion_url": r["page_url"],
    } for r in rows]


async def append_clip_note(page_id: str, note: str, user_id: int | None = None,
                           *, store: ClipStore | None = None) -> dict:
    if not note or not note.strip():
        raise ValueError("批注内容不能为空")
    ctx = _resolve_tenant(user_id)
    st = store or get_store()
    clip = st.get_clip(page_id, tenant_id=ctx.user_id)
    if not clip:
        raise PermissionError("未找到该页面或无权修改")
    page_url, _ = await notion_writer.enrich_notion_page(
        clip["page_id"], source_type="MCP", user_note=note.strip(),
        source_key=f"mcp-{abs(hash(note.strip()))}", scope=ctx.notion_scope(),
    )
    merged_note = "\n".join(filter(None, (clip.get("user_note"), note.strip())))
    st.upsert_clip(
        page_id=clip["page_id"], page_url=page_url, tenant_id=ctx.user_id,
        title=clip["title"], category=clip["category"], tags=clip["tags"],
        source=clip["source"], status=clip["status"], summary=clip["summary"],
        content_preview=clip["content_preview"], user_note=merged_note,
    )
    return {"ok": True, "page_id": clip["page_id"], "notion_url": page_url}


async def handle_jsonrpc_request(request: dict[str, Any], *, store: ClipStore | None = None) -> dict[str, Any]:
    req_id = request.get("id")
    method = request.get("method")
    params = request.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "tg-notion-clipper", "version": "2.5.0"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS_SPEC}}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        dispatch = {
            "search_clips": lambda: search_clips(**args, store=store),
            "get_clip_content": lambda: get_clip_content(**args, store=store),
            "list_recent_clips": lambda: list_recent_clips(**args, store=store),
            "append_clip_note": lambda: append_clip_note(**args, store=store),
        }
        if name not in dispatch:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unknown tool: {name}"}}
        try:
            res = await dispatch[name]()
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
            }}
        except Exception as exc:
            return {"jsonrpc": "2.0", "id": req_id, "result": {
                "isError": True,
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
            }}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Unsupported method: {method}"}}


def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if "id" not in req:
            continue
        resp = asyncio.run(handle_jsonrpc_request(req))
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    serve_stdio()
