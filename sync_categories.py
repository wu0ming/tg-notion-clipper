"""
Description: 把 rules.yaml 里有、但 Notion「分类配置」表里还没有的分类追加进去（只新增；不修改、不删除已有行）
  日常增删改分类请直接在 Notion 表里操作；这个脚本只在“我在 rules.yaml 里替你写好了新分类”时用
用法：python sync_categories.py
Changelog:
- v1.2 (2026-11): 新增
"""
import asyncio
import sys

import httpx

from config import env
from category_config import load_yaml_rules, _plain, F_NAME, F_DESC, F_FOCUS, F_TAGS, F_ENABLED, F_ORDER
from notion_writer import resolve_data_source_id, _request


def rt(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": (text or "")[:1900]}}]


async def main() -> None:
    if not env("NOTION_DB_CONFIG"):
        sys.exit("未配置 NOTION_DB_CONFIG，请先运行 python setup_notion.py")
    async with httpx.AsyncClient() as client:
        ds = await resolve_data_source_id(client, env("NOTION_DB_CONFIG"))
        pages, cursor = [], None
        while True:
            body = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
            data = await _request(client, "POST", f"/data_sources/{ds}/query", json=body)
            pages += data.get("results", [])
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        existing = {_plain(p["properties"].get(F_NAME)) for p in pages}
        orders = [(p["properties"].get(F_ORDER) or {}).get("number") for p in pages]
        next_order = max([o for o in orders if isinstance(o, (int, float))] or [0]) + 1
        print(f"配置表现有 {len(existing)} 行：" + "、".join(sorted(existing)))
        added = 0
        for c in load_yaml_rules():
            if c["name"] in existing:
                continue
            await _request(client, "POST", "/pages", json={
                "parent": {"type": "data_source_id", "data_source_id": ds},
                "properties": {
                    F_NAME: {"title": rt(c["name"])},
                    F_DESC: {"rich_text": rt(c["description"])},
                    F_FOCUS: {"rich_text": rt(c["extraction_focus"])},
                    F_TAGS: {"rich_text": rt(c["tagging_rule"])},
                    F_ENABLED: {"checkbox": True},
                    F_ORDER: {"number": next_order},
                }})
            print(f"已新增「{c['name']}」（排序 {next_order}）")
            next_order += 1
            added += 1
        print(f"完成，新增 {added} 个分类。" if added else "没有需要新增的分类。")


if __name__ == "__main__":
    asyncio.run(main())
