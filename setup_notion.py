"""
Description: 一键创建 Notion「归档总表」和「分类配置」表，把 rules.yaml 的分类写进配置表，并把两个库的 ID 回填 .env
前置：.env 中已填 NOTION_TOKEN 与 NOTION_PARENT_PAGE_ID（一个已在 ··· → Connections 中授权给 Integration 的页面）
用法：python setup_notion.py [--with-inbox]   （已配置的库会跳过）
Changelog:
- v1.1 (2026-11): 新增，按分类建 4 个库
- v1.2 (2026-11): 改为单库方案：1 张归档总表 + 1 张分类配置表（配置表首次创建时写入 rules.yaml 里的分类）
"""
import argparse
import json
import re
import sys

import httpx

from config import env, BASE_DIR
from category_config import load_yaml_rules, F_NAME, F_DESC, F_FOCUS, F_TAGS, F_ENABLED, F_ORDER, F_SUGGESTED_TAGS
from notion_writer import NOTION_API, NOTION_VERSION, _clean_id

ENV_PATH = BASE_DIR / ".env"
SOURCES = ["X", "Reddit", "V2EX", "WeChat", "GitHub", "YouTube", "Bilibili", "Web", "Note", "Telegram"]
STATUSES = [("PASS", "green"), ("PARTIAL", "orange"), ("FALLBACK", "red"), ("AI_PENDING", "yellow")]
INPUT_TYPES = ["Link", "Text", "Image", "Voice", "PDF", "Video", "Album"]
FETCH_TIERS = ["Tier1-API", "Tier2-Chain", "Tier3-GeminiWeb", "Tier4-Fallback", "Direct-Multimodal", "Browser-Markdown", "Video-Pipeline"]
ENTITY_TYPES = ["技术开源", "行业研报/政策法规", "深度长文/博客", "视频教程/访谈", "社媒观点", "薅羊毛/优惠/工具书签", "通用"]


def archive_schema(category_names: list[str]) -> dict:
    return {
        "Name": {"title": {}},
        "Category": {"select": {"options": [{"name": n} for n in category_names]}},
        "Tags": {"multi_select": {}},
        "Source": {"select": {"options": [{"name": s} for s in SOURCES]}},
        "URL": {"url": {}},
        "Summary": {"rich_text": {}},
        "Status": {"select": {"options": [{"name": n, "color": c} for n, c in STATUSES]}},
        "Created": {"date": {}},
        "InputType": {"select": {"options": [{"name": value} for value in INPUT_TYPES]}},
        "FetchTier": {"select": {"options": [{"name": value} for value in FETCH_TIERS]}},
        "EntityType": {"select": {"options": [{"name": value} for value in ENTITY_TYPES]}},
        "TelegramKey": {"rich_text": {}},
        "ClipKey": {"rich_text": {}},
        "ProjectKey": {"rich_text": {}},
        "AlbumKey": {"rich_text": {}},
    }


CONFIG_SCHEMA = {
    F_NAME: {"title": {}},
    F_DESC: {"rich_text": {}},
    F_FOCUS: {"rich_text": {}},
    F_TAGS: {"rich_text": {}},
    F_SUGGESTED_TAGS: {"multi_select": {}},
    F_ENABLED: {"checkbox": {}},
    F_ORDER: {"number": {"format": "number"}},
}


def write_env(key: str, value: str) -> None:
    text = ENV_PATH.read_text(encoding="utf-8-sig") if ENV_PATH.exists() else ""
    if re.search(rf"^{key}=.*$", text, flags=re.MULTILINE):
        text = re.sub(rf"^{key}=.*$", lambda m: f"{key}={value}", text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def rt(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": (text or "")[:1900]}}]


def create_db(client: httpx.Client, parent: str, title: str, emoji: str, props: dict) -> tuple[str, str]:
    resp = client.post(f"{NOTION_API}/databases", json={
        "parent": {"type": "page_id", "page_id": _clean_id(parent)},
        "title": [{"type": "text", "text": {"content": title}}],
        "icon": {"type": "emoji", "emoji": emoji},
        "initial_data_source": {"properties": props},
    })
    if resp.status_code >= 400:
        sys.exit(f"创建「{title}」失败：{resp.status_code} {resp.text[:300]}\n"
                 "常见原因：父页面未在 ··· → Connections 授权给 Integration，或 Integration 缺少 Insert content 权限。")
    data = resp.json()
    db_id = data["id"].replace("-", "")
    sources = data.get("data_sources") or client.get(f"{NOTION_API}/databases/{db_id}").json().get("data_sources", [])
    return db_id, sources[0]["id"]


def _seed_config(client: httpx.Client, ds_id: str, cats: list[dict]) -> None:
    for i, c in enumerate(cats, 1):
        resp = client.post(f"{NOTION_API}/pages", json={
            "parent": {"type": "data_source_id", "data_source_id": ds_id},
            "properties": {
                F_NAME: {"title": rt(c["name"])},
                F_DESC: {"rich_text": rt(c["description"])},
                F_FOCUS: {"rich_text": rt(c["extraction_focus"])},
                F_TAGS: {"rich_text": rt(c["tagging_rule"])},
                F_SUGGESTED_TAGS: {"multi_select": [{"name": tag} for tag in c["suggested_tags"]]},
                F_ENABLED: {"checkbox": True},
                F_ORDER: {"number": i},
            }})
        state = "已写入" if resp.status_code < 400 else f"写入失败 {resp.status_code} {resp.text[:150]}"
        print(f"  分类 {i}「{c['name']}」{state}")


def upgrade_archive_schema(client: httpx.Client, db_id: str, label: str) -> None:
    from notion_writer import _clean_id
    cleaned = _clean_id(db_id)
    db_resp = client.get(f"{NOTION_API}/databases/{cleaned}")
    if db_resp.status_code < 400:
        ds_id = (db_resp.json().get("data_sources") or [{}])[0].get("id") or cleaned
    else:
        ds_id = cleaned
    ds_resp = client.get(f"{NOTION_API}/data_sources/{ds_id}")
    if ds_resp.status_code >= 400:
        sys.exit(f"读取「{label}」结构失败：{ds_resp.status_code} {ds_resp.text[:200]}")
    existing = ds_resp.json().get("properties", {})
    patch_props: dict = {}

    desired_new = {
        "EntityType": {"select": {"options": [{"name": value} for value in ENTITY_TYPES]}},
        "ProjectKey": {"rich_text": {}},
        "AlbumKey": {"rich_text": {}},
    }
    for col, spec in desired_new.items():
        if col not in existing:
            patch_props[col] = spec

    select_additions = {
        "Source": SOURCES,
        "InputType": INPUT_TYPES,
        "FetchTier": FETCH_TIERS,
    }
    for col, needed in select_additions.items():
        if col in existing and existing[col].get("type") == "select":
            cur_opts = existing[col]["select"].get("options", [])
            cur_names = {o.get("name") for o in cur_opts}
            missing_opts = [{"name": n} for n in needed if n not in cur_names]
            if missing_opts:
                preserved = [{"id": o["id"], "name": o["name"], "color": o.get("color", "default")}
                             if "id" in o else {"name": o["name"]} for o in cur_opts]
                patch_props[col] = {"select": {"options": preserved + missing_opts}}

    if not patch_props:
        print(f"「{label}」字段与选项已全部齐备，无需变更。")
        return
    resp = client.patch(f"{NOTION_API}/data_sources/{ds_id}", json={"properties": patch_props})
    if resp.status_code >= 400:
        sys.exit(f"升级「{label}」字段失败：{resp.status_code} {resp.text[:300]}")
    print(f"已无损补齐「{label}」字段/选项：{', '.join(patch_props.keys())}")


def main(with_inbox: bool = False, user_id: str = "", parent_page: str = "", upgrade: bool = False) -> None:
    token = env("NOTION_TOKEN")
    if not token:
        sys.exit("请先在 .env 填写 NOTION_TOKEN")
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
    if upgrade:
        with httpx.Client(headers=headers, timeout=30.0) as client:
            if arch := env("NOTION_DB_ARCHIVE"):
                upgrade_archive_schema(client, arch, "归档总表")
            if inbox := env("NOTION_DB_INBOX"):
                upgrade_archive_schema(client, inbox, "归档收件箱")
        return
    parent = parent_page or env("NOTION_PARENT_PAGE_ID")
    if not parent:
        sys.exit("请先在 .env 填写 NOTION_PARENT_PAGE_ID（或传入 --parent-page）")
    cats = load_yaml_rules()
    names = [c["name"] for c in cats]
    with httpx.Client(headers=headers, timeout=30.0) as client:
        if user_id:
            label = f"用户 {user_id}"
            archive_id, _ = create_db(client, parent, f"归档总表 ({label})", "🗂️", archive_schema(names))
            config_id, ds_id = create_db(client, parent, f"分类配置 ({label})", "⚙️", CONFIG_SCHEMA)
            _seed_config(client, ds_id, cats)
            inbox_id = ""
            if with_inbox:
                inbox_id, _ = create_db(client, parent, f"归档收件箱 ({label})", "📥", archive_schema(names))
            snippet = {user_id: {"archive_db": archive_id, "config_db": config_id, **({"inbox_db": inbox_id} if inbox_id else {})}}
            print("已创建独立用户库，请将以下条目合并到 USER_CONFIGS：")
            print(json.dumps(snippet, ensure_ascii=False))
            return

        if env("NOTION_DB_ARCHIVE"):
            print("跳过「归档总表」：NOTION_DB_ARCHIVE 已配置")
        else:
            db_id, _ = create_db(client, parent, "归档总表", "🗂️", archive_schema(names))
            write_env("NOTION_DB_ARCHIVE", db_id)
            print(f"已创建「归档总表」→ NOTION_DB_ARCHIVE={db_id}")

        if with_inbox and not env("NOTION_DB_INBOX"):
            db_id, _ = create_db(client, parent, "归档收件箱", "📥", archive_schema(names))
            write_env("NOTION_DB_INBOX", db_id)
            print(f"已创建「归档收件箱」→ NOTION_DB_INBOX={db_id}")

        if env("NOTION_DB_CONFIG"):
            print("跳过「分类配置」：NOTION_DB_CONFIG 已配置")
        else:
            db_id, ds_id = create_db(client, parent, "分类配置", "⚙️", CONFIG_SCHEMA)
            write_env("NOTION_DB_CONFIG", db_id)
            print(f"已创建「分类配置」→ NOTION_DB_CONFIG={db_id}")
            _seed_config(client, ds_id, cats)
    print("完成。之后在 Notion「分类配置」表里直接增删改分类即可，机器人 5 分钟内生效（发 /reload 立即生效）。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="创建或升级 Notion 归档总表和分类配置表")
    parser.add_argument("--with-inbox", action="store_true", help="同时创建独立的归档收件箱")
    parser.add_argument("--user-id", default="", help="为指定 Telegram 用户 ID 创建独立知识库并输出 USER_CONFIGS 片段")
    parser.add_argument("--parent-page", default="", help="覆盖父页面 ID/链接")
    parser.add_argument("--upgrade", action="store_true", help="无损补齐已有归档表的 EntityType/ProjectKey/AlbumKey 与新选项")
    args = parser.parse_args()
    main(args.with_inbox, args.user_id, args.parent_page, args.upgrade)
