"""
Description: Notion 自定义「剪藏模板」页面加载、5 分钟缓存与所见即所得 Block 渲染引擎
Changelog:
- v3.1 (2026-11): 新增：自动发现「信息归档」下的「剪藏模板 / 检查模板」页面并缓存 5 分钟，支持 /reload 立即刷新与自由增删改 Notion 块
"""
from __future__ import annotations

from copy import deepcopy
import logging
import re
import time
from typing import Any

import httpx

from config import env
from utils import chunk_text, rich_text, truncate

CACHE_TTL = 300
TEMPLATE_PAGE_NAMES = ("剪藏模板", "检查模板")
TEMPLATE_HELP_PREFIXES = ("使用说明", "模板说明", "[说明]")
VAR_RE = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
BLOCK_VARS = frozenset({
    "key_points", "raw_content", "extracted_text", "entity_sections",
    "video_steps", "forwarded_section", "forwarded_text", "forward_source",
    "reference_links", "media_metadata", "attachments",
})
TEXT_BLOCK_TYPES = frozenset({
    "paragraph", "heading_1", "heading_2", "heading_3", "callout",
    "quote", "bulleted_list_item", "numbered_list_item", "to_do", "toggle", "code",
})
CONTAINER_BLOCK_TYPES = frozenset({
    "heading_1", "heading_2", "heading_3", "toggle", "callout",
    "quote", "bulleted_list_item", "numbered_list_item", "to_do",
})
log = logging.getLogger("clipper")

_template_states: dict[str, dict[str, Any]] = {}


def clear_template_cache() -> None:
    _template_states.clear()


def _plain_rich_text(items: list[dict] | None) -> str:
    return "".join(p.get("plain_text") or (p.get("text") or {}).get("content", "") for p in (items or []))


def _block_text(block: dict) -> str:
    btype = block.get("type", "")
    body = block.get(btype) or {}
    if btype == "bookmark":
        return str(body.get("url") or "")
    return _plain_rich_text(body.get("rich_text"))


def _clean_rich_text_items(items: list[dict] | None) -> list[dict]:
    cleaned: list[dict] = []
    for item in items or []:
        if item.get("type") != "text":
            plain = item.get("plain_text") or ""
            if plain:
                entry: dict[str, Any] = {"type": "text", "text": {"content": truncate(plain, 1900)}}
                if item.get("annotations"):
                    entry["annotations"] = deepcopy(item["annotations"])
                cleaned.append(entry)
            continue
        text_obj = item.get("text") or {}
        content = text_obj.get("content") if text_obj.get("content") is not None else item.get("plain_text", "")
        new_item: dict[str, Any] = {"type": "text", "text": {"content": str(content)}}
        if link := text_obj.get("link"):
            new_item["text"]["link"] = deepcopy(link)
        if item.get("annotations"):
            new_item["annotations"] = deepcopy(item["annotations"])
        cleaned.append(new_item)
    return cleaned


def _substitute_rich_text(items: list[dict], scalars: dict[str, str]) -> tuple[list[dict], list[str], bool]:
    """替换 rich_text 中的 {{var}}，保留原有格式（加粗/颜色/链接）；返回 (新 rich_text, 引用的变量名, 是否有非空替换)。"""
    found_vars: list[str] = []
    has_nonempty_var = False
    out: list[dict] = []

    for item in _clean_rich_text_items(items):
        content = item["text"]["content"]
        annotations = item.get("annotations")
        orig_link = item["text"].get("link")
        pos = 0
        for match in VAR_RE.finditer(content):
            if match.start() > pos:
                seg = content[pos:match.start()]
                seg_item: dict[str, Any] = {"type": "text", "text": {"content": seg}}
                if orig_link:
                    seg_item["text"]["link"] = deepcopy(orig_link)
                if annotations:
                    seg_item["annotations"] = deepcopy(annotations)
                out.append(seg_item)
            var_name = match.group(1)
            found_vars.append(var_name)
            val = str(scalars.get(var_name, "") or "")
            if val:
                has_nonempty_var = True
                val_item: dict[str, Any] = {"type": "text", "text": {"content": truncate(val, 1900)}}
                if var_name in {"url", "original_url"} and val.startswith(("http://", "https://")):
                    val_item["text"]["link"] = {"url": val[:2000]}
                elif orig_link:
                    val_item["text"]["link"] = deepcopy(orig_link)
                if annotations:
                    val_item["annotations"] = deepcopy(annotations)
                out.append(val_item)
            pos = match.end()
        if pos < len(content):
            tail = content[pos:]
            tail_item: dict[str, Any] = {"type": "text", "text": {"content": tail}}
            if orig_link:
                tail_item["text"]["link"] = deepcopy(orig_link)
            if annotations:
                tail_item["annotations"] = deepcopy(annotations)
            out.append(tail_item)

    merged: list[dict] = []
    for item in out:
        text_val = item["text"]["content"]
        if not text_val:
            continue
        if (
            merged
            and merged[-1]["text"].get("link") == item["text"].get("link")
            and merged[-1].get("annotations") == item.get("annotations")
            and len(merged[-1]["text"]["content"]) + len(text_val) <= 1900
        ):
            merged[-1]["text"]["content"] += text_val
            continue
        if len(text_val) > 1900:
            text_val = truncate(text_val, 1900)
            item["text"]["content"] = text_val
        merged.append(item)
    return merged, found_vars, has_nonempty_var


def _clone_block_shell(block: dict, *, rich_text_items: list[dict] | None = None,
                       children: list[dict] | None = None) -> dict | None:
    btype = block.get("type", "")
    body = block.get(btype) or {}
    if btype == "divider":
        return {"object": "block", "type": "divider", "divider": {}}
    if btype == "bookmark":
        url = str(body.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return None
        new_body: dict[str, Any] = {"url": url[:2000]}
        if body.get("caption"):
            new_body["caption"] = _clean_rich_text_items(body["caption"])
        return {"object": "block", "type": "bookmark", "bookmark": new_body}
    if btype not in TEXT_BLOCK_TYPES:
        return None

    new_body = {"rich_text": rich_text_items if rich_text_items is not None else _clean_rich_text_items(body.get("rich_text"))}
    if btype in {"heading_1", "heading_2", "heading_3"}:
        if body.get("is_toggleable"):
            new_body["is_toggleable"] = True
        if body.get("color") and body["color"] != "default":
            new_body["color"] = body["color"]
    elif btype == "callout":
        if body.get("icon"):
            new_body["icon"] = deepcopy(body["icon"])
        if body.get("color") and body["color"] != "default":
            new_body["color"] = body["color"]
    elif btype == "quote":
        if body.get("color") and body["color"] != "default":
            new_body["color"] = body["color"]
    elif btype == "to_do":
        new_body["checked"] = bool(body.get("checked", False))
    elif btype == "code":
        new_body["language"] = body.get("language") or "plain text"
    elif btype in {"paragraph", "bulleted_list_item", "numbered_list_item", "toggle"}:
        if body.get("color") and body["color"] != "default":
            new_body["color"] = body["color"]

    if children:
        new_body["children"] = children
    return {"object": "block", "type": btype, btype: new_body}


def _is_doc_block(block: dict) -> bool:
    text = _block_text(block).strip()
    return any(text.startswith(prefix) for prefix in TEMPLATE_HELP_PREFIXES)


def _match_template_title(title: str) -> int:
    clean = (title or "").strip()
    if clean == TEMPLATE_PAGE_NAMES[0]:
        return 0
    if clean == TEMPLATE_PAGE_NAMES[1]:
        return 1
    if any(name in clean for name in TEMPLATE_PAGE_NAMES):
        return 2
    return 99


async def _find_child_template_page(client: httpx.AsyncClient, parent_id: str, *, token: str = "") -> str:
    from notion_writer import NotionError, _clean_id, _request
    pid = _clean_id(parent_id)
    if not pid:
        return ""
    try:
        data = await _request(client, "GET", f"/blocks/{pid}/children", token=token, params={"page_size": 100})
    except (NotionError, httpx.HTTPError):
        return ""
    candidates: list[tuple[int, str]] = []
    for block in data.get("results") or []:
        if block.get("type") == "child_page":
            title = str((block.get("child_page") or {}).get("title") or "")
            rank = _match_template_title(title)
            if rank < 99:
                candidates.append((rank, _clean_id(block["id"])))
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1] if candidates else ""


async def _resolve_template_page_id(client: httpx.AsyncClient, *, template_page: str = "",
                                     archive_db: str = "", notion_token: str = "") -> str:
    from notion_writer import NotionError, _clean_id, _request
    explicit = _clean_id(template_page or (env("NOTION_PAGE_TEMPLATE") if not notion_token else ""))
    if explicit:
        return explicit

    parent_candidates: list[str] = []
    target_db = _clean_id(archive_db or env("NOTION_DB_ARCHIVE"))
    if target_db:
        try:
            db = await _request(client, "GET", f"/databases/{target_db}", token=notion_token)
            db_parent = db.get("parent") or {}
            if db_parent.get("type") == "page_id" and db_parent.get("page_id"):
                parent_candidates.append(_clean_id(db_parent["page_id"]))
        except (NotionError, httpx.HTTPError):
            pass

    if not notion_token and (env_parent := env("NOTION_PARENT_PAGE_ID")):
        cleaned = _clean_id(env_parent)
        if cleaned and cleaned not in parent_candidates:
            parent_candidates.append(cleaned)

    for pid in parent_candidates:
        if found := await _find_child_template_page(client, pid, token=notion_token):
            return found
    return ""


async def _fetch_block_tree(client: httpx.AsyncClient, block_id: str, *, token: str = "", depth: int = 0) -> list[dict]:
    from notion_writer import _list_blocks
    raw_blocks = await _list_blocks(client, block_id, token=token)
    out: list[dict] = []
    for block in raw_blocks:
        if depth == 0 and _is_doc_block(block):
            continue
        btype = block.get("type", "")
        if block.get("has_children") and btype in CONTAINER_BLOCK_TYPES and depth < 2:
            sub = await _fetch_block_tree(client, block["id"], token=token, depth=depth + 1)
            body = deepcopy(block.get(btype) or {})
            body["children"] = sub
            copied = dict(block)
            copied[btype] = body
            out.append(copied)
        else:
            out.append(block)
    return out


async def get_template(force: bool = False, *, template_page: str | None = None,
                       archive_db: str = "", notion_token: str = "") -> tuple[list[dict] | None, str, str]:
    """返回 (模板 Block 列表或 None, 来源说明, 模板页面 ID)。支持按租户/数据库隔离 5 分钟缓存。"""
    from notion_writer import _clean_id, _get_client
    explicit_page = _clean_id(template_page or "") if template_page is not None else _clean_id(env("NOTION_PAGE_TEMPLATE"))
    scope_db = _clean_id(archive_db or env("NOTION_DB_ARCHIVE"))
    cache_key = f"{explicit_page or scope_db}:{bool(notion_token)}"
    state = _template_states.setdefault(
        cache_key, {"blocks": None, "loaded_at": 0.0, "source": "系统默认模板", "page_id": ""}
    )
    if not force and state["loaded_at"] and time.time() - state["loaded_at"] < CACHE_TTL:
        return state["blocks"], state["source"], state["page_id"]

    client = _get_client()
    try:
        page_id = state["page_id"] if (not force and state["page_id"]) else ""
        if not page_id:
            page_id = await _resolve_template_page_id(
                client, template_page=explicit_page, archive_db=scope_db, notion_token=notion_token
            )
        if not page_id:
            state.update(blocks=None, loaded_at=time.time(), source="系统默认模板（未检测到剪藏模板页）", page_id="")
            return None, state["source"], ""
        blocks = await _fetch_block_tree(client, page_id, token=notion_token)
        if blocks:
            state.update(
                blocks=blocks, loaded_at=time.time(),
                source=f"Notion 剪藏模板（{len(blocks)} 个顶层区块）", page_id=page_id,
            )
            return blocks, state["source"], page_id
        state.update(blocks=None, loaded_at=time.time(), source="系统默认模板（模板页为空）", page_id=page_id)
        return None, state["source"], page_id
    except Exception as exc:
        log.warning("读取 Notion 剪藏模板失败：%s", type(exc).__name__)
        if state["blocks"] is not None:
            state["loaded_at"] = time.time()
            return state["blocks"], "Notion 剪藏模板（缓存，最近一次读取失败）", state["page_id"]
        state.update(blocks=None, loaded_at=time.time(), source="系统默认模板（读取失败回退）", page_id="")
        return None, state["source"], ""


def render_template_blocks(
    template_blocks: list[dict],
    scalars: dict[str, str],
    block_providers: dict[str, list[dict]],
) -> tuple[list[dict], set[str]]:
    """根据模板 Block 树与运行时素材渲染 Notion Children，并自动清理空变量与空标题。"""
    used_providers: set[str] = set()

    def render_node(block: dict) -> tuple[list[dict], bool]:
        """返回 (渲染出的块列表, 该节点是否包含占位符)。"""
        btype = block.get("type", "")
        if _is_doc_block(block):
            return [], True

        body = block.get(btype) or {}
        raw_text = _block_text(block).strip()
        single_match = VAR_RE.fullmatch(raw_text)
        if single_match and single_match.group(1) in BLOCK_VARS:
            key = single_match.group(1)
            used_providers.add(key)
            return deepcopy(block_providers.get(key) or []), True
        if btype == "paragraph" and single_match and single_match.group(1) in {"url", "original_url"}:
            key = single_match.group(1)
            used_providers.add(key)
            val = str(scalars.get(key, "") or "").strip()
            if val.startswith(("http://", "https://")):
                return [{"object": "block", "type": "bookmark", "bookmark": {"url": val[:2000]}}], True
            return [], True

        if btype == "bookmark":
            raw_url = str(body.get("url") or "").strip()
            refs = VAR_RE.findall(raw_url)
            used_providers.update(refs)
            resolved_url = VAR_RE.sub(lambda m: str(scalars.get(m.group(1), "") or ""), raw_url).strip()
            if refs and not resolved_url:
                return [], True
            cloned = _clone_block_shell({"type": "bookmark", "bookmark": {**body, "url": resolved_url}})
            return ([cloned] if cloned else []), bool(refs)

        rendered_children: list[dict] = []
        children_had_var = False
        orig_children = body.get("children") or []
        if orig_children:
            rendered_children, children_had_var = render_sequence(orig_children)
            if children_had_var and not rendered_children:
                # 若内部完全由动态占位符构成且全部为空，则连同外层 Toggle/Heading 一并隐藏
                return [], True

        if btype not in TEXT_BLOCK_TYPES:
            cloned = _clone_block_shell(block, children=rendered_children or None)
            return ([cloned] if cloned else []), children_had_var

        new_rt, found_vars, has_nonempty = _substitute_rich_text(body.get("rich_text") or [], scalars)
        if found_vars:
            used_providers.update(found_vars)
            if not has_nonempty and not rendered_children:
                return [], True
        cloned = _clone_block_shell(block, rich_text_items=new_rt, children=rendered_children or None)
        return ([cloned] if cloned else []), bool(found_vars or children_had_var)

    def render_sequence(nodes: list[dict]) -> tuple[list[dict], bool]:
        entries: list[tuple[list[dict], bool, bool]] = []
        any_dynamic = False
        for node in nodes:
            if node.get("type") == "divider":
                entries.append(([{"object": "block", "type": "divider", "divider": {}}], False, True))
                continue
            btype = node.get("type", "")
            body = node.get(btype) or {}
            is_flat_heading = (
                btype in {"heading_1", "heading_2", "heading_3"}
                and not body.get("is_toggleable")
                and not (body.get("children") or [])
                and not VAR_RE.search(_block_text(node))
            )
            rendered, is_dynamic = render_node(node)
            any_dynamic = any_dynamic or is_dynamic
            entries.append((rendered, is_dynamic, is_flat_heading))

        # 若平级静态 Heading 紧接的是动态块且全部为空（直到下一个 Heading/Divider 都没有任何有效块），省略空 Heading
        out: list[dict] = []
        for idx, (rendered, is_dynamic, is_flat_heading) in enumerate(entries):
            if is_flat_heading and rendered:
                has_content_after = False
                saw_dynamic_after = False
                for nxt_rendered, nxt_dynamic, nxt_boundary in entries[idx + 1:]:
                    if nxt_boundary:
                        break
                    if nxt_dynamic:
                        saw_dynamic_after = True
                    if nxt_rendered:
                        has_content_after = True
                        break
                if saw_dynamic_after and not has_content_after:
                    continue
            out.extend(rendered)
        return out, any_dynamic

    final_blocks, _ = render_sequence(template_blocks)
    return final_blocks, used_providers


def default_template_seed_blocks() -> list[dict]:
    """生成写入 Notion「剪藏模板」页面的初始区块（含可折叠变量说明）。"""
    help_lines = [
        "1. 在本页自由调整区块顺序、更改标题、更换 Callout 颜色/图标、删除不想要的模块，或添加固定待办/分割线。",
        "2. 花括号 {{变量名}} 会自动替换为实际剪藏内容；当对应内容为空时，所在行/折叠块会自动隐藏。",
        "3. 修改后最多 5 分钟自动生效，或在 Telegram 私聊 Bot 发送 /reload 立即生效。",
        "4. 常用行内变量：{{user_note}}（个人批注）、{{summary}}（AI 摘要）、{{url}}、{{category}}、{{tags}}、{{source}}、{{origin}}、{{channel_meta}}、{{status_notice}}、{{date}}。",
        "5. 常用整块展开变量：{{key_points}}、{{entity_sections}}、{{video_steps}}、{{forwarded_section}}、{{forward_source}}、{{raw_content}}、{{extracted_text}}、{{reference_links}}、{{media_metadata}}、{{attachments}}。",
    ]
    return [
        {
            "object": "block", "type": "heading_2",
            "heading_2": {
                "rich_text": rich_text("使用说明（不会写入剪藏页，点击展开查看变量字典）"),
                "is_toggleable": True,
                "children": [
                    {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich_text(line)}}
                    for line in help_lines
                ],
            },
        },
        {"object": "block", "type": "divider", "divider": {}},
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": rich_text("个人批注：{{user_note}}"),
                "icon": {"type": "emoji", "emoji": "📝"},
                "color": "yellow_background",
            },
        },
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": rich_text("随附说明（来源未确认）：{{accompanying_text}}"),
                "icon": {"type": "emoji", "emoji": "💬"},
            },
        },
        {
            "object": "block", "type": "callout",
            "callout": {
                "rich_text": rich_text("{{status_notice}}"),
                "icon": {"type": "emoji", "emoji": "⚠️"},
            },
        },
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{url}}")}},
        {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich_text("核心要点")}},
        {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich_text("{{key_points}}")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{entity_sections}}")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{video_steps}}")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{forwarded_section}}")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{forward_source}}")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{reference_links}}")}},
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("输入与归档渠道：{{channel_meta}}")}},
        {
            "object": "block", "type": "heading_2",
            "heading_2": {
                "rich_text": rich_text("原始链接"),
                "is_toggleable": True,
                "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{original_url}}")}}],
            },
        },
        {
            "object": "block", "type": "heading_2",
            "heading_2": {
                "rich_text": rich_text("处理记录"),
                "is_toggleable": True,
                "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{fetch_note}}")}}],
            },
        },
        {
            "object": "block", "type": "heading_2",
            "heading_2": {
                "rich_text": rich_text("抓取正文存档（点击展开）"),
                "is_toggleable": True,
                "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{raw_content}}")}}],
            },
        },
        {
            "object": "block", "type": "heading_2",
            "heading_2": {
                "rich_text": rich_text("提取文字 / 语音转写"),
                "is_toggleable": True,
                "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{extracted_text}}")}}],
            },
        },
        {
            "object": "block", "type": "heading_2",
            "heading_2": {
                "rich_text": rich_text("Telegram 媒体元数据"),
                "is_toggleable": True,
                "children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{media_metadata}}")}}],
            },
        },
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text("{{attachments}}")}},
    ]


def build_template_scalars(
    *,
    result,
    source_type: str,
    url: str,
    status: str,
    user_note: str,
    accompanying_text: str,
    fetch_note: str,
    input_type: str,
    fetch_tier: str,
    origin_label: str,
    channel_details: str,
    original_url: str,
    created_iso: str,
    project_key: str,
    entity_type: str,
) -> dict[str, str]:
    status_notice = ""
    if status != "PASS":
        label = "处理状态" if status == "AI_PENDING" else "抓取状态"
        status_notice = f"{label}：{status}" + (f"（{fetch_note}）" if fetch_note else "")
    tags_str = " ".join(f"#{t}" for t in (getattr(result, "tags", None) or []))
    return {
        "title": str(getattr(result, "title", "") or ""),
        "category": str(getattr(result, "category", "") or ""),
        "tags": tags_str,
        "source": str(source_type or ""),
        "input_type": str(input_type or ""),
        "fetch_tier": str(fetch_tier or ""),
        "origin": str(origin_label or ""),
        "channel_meta": str(channel_details or ""),
        "url": str(url or ""),
        "original_url": str(original_url or "") if (original_url and (original_url != url or len(original_url) > 2000)) else "",
        "summary": str(getattr(result, "summary", "") or ""),
        "user_note": "" if accompanying_text else str(user_note or ""),
        "accompanying_text": str(accompanying_text or ""),
        "status": str(status or ""),
        "status_notice": status_notice,
        "fetch_note": str(fetch_note or ""),
        "created": created_iso[:16].replace("T", " "),
        "date": created_iso[:10],
        "project_key": str(project_key or ""),
        "entity_type": str(entity_type or ""),
    }


def paragraph_chunks(text: str, *, limit: int = 1800, max_chunks: int = 24) -> list[dict]:
    return [
        {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(piece, 2000)}}
        for piece in chunk_text(text or "", limit)[:max_chunks]
    ]
