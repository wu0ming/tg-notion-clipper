"""
Description: 分类配置加载。优先读 Notion「分类配置」表（你在 Notion 里直接增删改行即可生效），5 分钟缓存；
  读取失败时沿用上一次从 Notion 成功读到的分类；未配置或从未读到时才用 rules.yaml 兜底
  配置表字段：分类名 / 描述 / 提取重点 / 标签池 / 建议标签（可选） / 启用 / 排序
Changelog:
- v1.2 (2026-11): 新增
"""
import asyncio
import logging
import re
import time
from pathlib import Path

import yaml

from config import env, BASE_DIR
from utils import clean_tags

CACHE_TTL = 300
RULES_PATH = BASE_DIR / "rules.yaml"
F_NAME, F_DESC, F_FOCUS, F_TAGS, F_ENABLED, F_ORDER = "分类名", "描述", "提取重点", "标签池", "启用", "排序"
F_SUGGESTED_TAGS = "建议标签"
log = logging.getLogger("clipper")

_state: dict = {"cats": None, "loaded_at": 0.0, "source": ""}
_lock = asyncio.Lock()


def _suggested_tags(value: str | list[str] | None) -> list[str]:
    tags = re.split(r"[,，、;；\r\n]+", value) if isinstance(value, str) else value or []
    return clean_tags(tags, limit=len(tags))


def global_suggested_tags() -> list[str]:
    return _suggested_tags(env("GLOBAL_SUGGESTED_TAGS"))


def load_yaml_rules(path: Path = RULES_PATH) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [{"name": c["name"], "description": c.get("description", ""),
             "extraction_focus": c.get("extraction_focus", ""), "tagging_rule": c.get("tagging_rule", ""),
             "suggested_tags": _suggested_tags(c.get("suggested_tags"))}
            for c in data["categories"]]


def _plain(prop: dict | None) -> str:
    if not prop:
        return ""
    items = prop.get(prop.get("type") or "", [])
    if isinstance(items, list):
        return "".join(i.get("plain_text", "") for i in items).strip()
    return ""


def parse_suggested_tags_property(prop: dict | None) -> list[str]:
    if prop and prop.get("type") == "multi_select":
        return _suggested_tags([item["name"] for item in prop.get("multi_select", []) if item.get("name")])
    return _suggested_tags(_plain(prop)) if prop and prop.get("type") == "rich_text" else []


def parse_config_rows(pages: list[dict]) -> list[dict]:
    """把配置表的页面整理成分类列表：只取已启用、名称非空的行；按排序升序；重名只留第一个"""
    rows = []
    for p in pages:
        props = p.get("properties", {})
        if not (props.get(F_ENABLED) or {}).get("checkbox", False):
            continue
        name = _plain(props.get(F_NAME)).replace(",", "、").strip()[:100]  # Notion 下拉选项不能含英文逗号
        if not name:
            continue
        order = (props.get(F_ORDER) or {}).get("number")
        rows.append({"name": name, "description": _plain(props.get(F_DESC)),
                     "extraction_focus": _plain(props.get(F_FOCUS)), "tagging_rule": _plain(props.get(F_TAGS)),
                     "suggested_tags": parse_suggested_tags_property(props.get(F_SUGGESTED_TAGS)),
                     "_order": order if isinstance(order, (int, float)) else 9999})
    rows.sort(key=lambda r: r["_order"])
    seen, out = set(), []
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        r.pop("_order")
        out.append(r)
    return out


async def _fetch_from_notion() -> list[dict]:
    from notion_writer import resolve_data_source_id, _request, _get_client
    client = _get_client()
    ds = await resolve_data_source_id(client, env("NOTION_DB_CONFIG"))
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = await _request(client, "POST", f"/data_sources/{ds}/query", json=body)
        pages += data.get("results", [])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return parse_config_rows(pages)


async def get_categories(force: bool = False) -> tuple[list[dict], str]:
    """返回 (启用的分类列表, 来源说明)"""
    async with _lock:
        if not force and _state["cats"] and time.time() - _state["loaded_at"] < CACHE_TTL:
            return _state["cats"], _state["source"]
        if not env("NOTION_DB_CONFIG"):
            cats = load_yaml_rules()
            _state.update(cats=cats, loaded_at=time.time(), source="rules.yaml")
            return cats, "rules.yaml"
        try:
            cats = await _fetch_from_notion()
            if cats:
                _state.update(cats=cats, loaded_at=time.time(), source="Notion 分类配置表")
                return cats, _state["source"]
            log.warning("分类配置表里没有启用的分类，沿用旧配置")
        except Exception as exc:
            log.warning("读取分类配置表失败：%s", exc)
        if _state["cats"] and _state["source"].startswith("Notion"):
            _state["loaded_at"] = time.time()  # 5 分钟后再试，避免每条消息都重试
            return _state["cats"], "Notion 分类配置表（缓存，最近一次读取失败）"
        cats = load_yaml_rules()
        _state.update(cats=cats, loaded_at=time.time(), source="rules.yaml（配置表暂不可用）")
        return cats, _state["source"]
