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

_state: dict = {"cats": None, "loaded_at": 0.0, "source": "", "mtime": 0.0}
_states: dict[str, dict] = {}
_domain_rules_cache: dict = {"mtime": 0.0, "rules": []}
_lock = asyncio.Lock()


def _suggested_tags(value: str | list[str] | None) -> list[str]:
    tags = re.split(r"[,，、;；\r\n]+", value) if isinstance(value, str) else value or []
    return clean_tags(tags, limit=len(tags))


def global_suggested_tags() -> list[str]:
    return _suggested_tags(env("GLOBAL_SUGGESTED_TAGS"))


def _yaml_mtime(path: Path = RULES_PATH) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def load_yaml_rules(path: Path = RULES_PATH) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [{"name": c["name"], "description": c.get("description", ""),
             "extraction_focus": c.get("extraction_focus", ""), "tagging_rule": c.get("tagging_rule", ""),
             "suggested_tags": _suggested_tags(c.get("suggested_tags"))}
            for c in data["categories"]]


def load_domain_rules(path: Path = RULES_PATH) -> list[dict]:
    """热加载 rules.yaml 中的确定性域名/路径 L0 路由规则。"""
    mtime = _yaml_mtime(path)
    if _domain_rules_cache["mtime"] == mtime and _domain_rules_cache["rules"] is not None:
        return _domain_rules_cache["rules"]
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rules = data.get("domain_rules") or []
    except Exception:
        rules = []
    _domain_rules_cache.update(mtime=mtime, rules=rules)
    return rules


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


async def _fetch_from_notion(config_db: str = "", notion_token: str = "") -> list[dict]:
    from notion_writer import resolve_data_source_id, _request, _get_client
    client = _get_client()
    db = config_db or env("NOTION_DB_CONFIG")
    ds = await resolve_data_source_id(client, db, token=notion_token)
    pages, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = await _request(client, "POST", f"/data_sources/{ds}/query", token=notion_token, json=body)
        pages += data.get("results", [])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return parse_config_rows(pages)


async def get_categories(force: bool = False, *, config_db: str | None = None,
                         notion_token: str = "") -> tuple[list[dict], str]:
    """返回 (启用的分类列表, 来源说明)。支持多用户按 config_db 隔离缓存与 rules.yaml 热加载。"""
    async with _lock:
        target_db = env("NOTION_DB_CONFIG") if config_db is None else config_db
        state = _state if config_db is None else _states.setdefault(
            f"{target_db}:{bool(notion_token)}", {"cats": None, "loaded_at": 0.0, "source": "", "mtime": 0.0}
        )
        yaml_mtime = _yaml_mtime()
        if not target_db:
            if not force and state["cats"] and state.get("mtime") == yaml_mtime:
                return state["cats"], state["source"]
            cats = load_yaml_rules()
            state.update(cats=cats, loaded_at=time.time(), source="rules.yaml", mtime=yaml_mtime)
            return cats, "rules.yaml"
        if not force and state["cats"] and time.time() - state["loaded_at"] < CACHE_TTL:
            return state["cats"], state["source"]
        try:
            cats = await _fetch_from_notion() if config_db is None and not notion_token else await _fetch_from_notion(target_db, notion_token)
            if cats:
                state.update(cats=cats, loaded_at=time.time(), source="Notion 分类配置表")
                return cats, state["source"]
            log.warning("分类配置表里没有启用的分类，沿用旧配置")
        except Exception as exc:
            log.warning("读取分类配置表失败：%s", exc)
        if state["cats"] and state["source"].startswith("Notion"):
            state["loaded_at"] = time.time()  # 5 分钟后再试，避免每条消息都重试
            return state["cats"], "Notion 分类配置表（缓存，最近一次读取失败）"
        cats = load_yaml_rules()
        state.update(cats=cats, loaded_at=time.time(), source="rules.yaml（配置表暂不可用）", mtime=yaml_mtime)
        return cats, state["source"]
