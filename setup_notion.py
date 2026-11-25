"""
Description: 一键创建 Notion「归档总表」和「分类配置」表，把 rules.yaml 的分类写进配置表，并把两个库的 ID 回填 .env
前置：.env 中已填 NOTION_TOKEN 与 NOTION_PARENT_PAGE_ID（一个已在 ··· → Connections 中授权给 Integration 的页面）
用法：python setup_notion.py [--with-inbox]   （已配置的库会跳过）
Changelog:
- v1.1 (2026-11): 新增，按分类建 4 个库
- v1.2 (2026-11): 改为单库方案：1 张归档总表 + 1 张分类配置表（配置表首次创建时写入 rules.yaml 里的分类）
"""
import argparse
import re
import sys

import httpx

from config import env, BASE_DIR
from category_config import load_yaml_rules, F_NAME, F_DESC, F_FOCUS, F_TAGS, F_ENABLED, F_ORDER, F_SUGGESTED_TAGS
from notion_writer import NOTION_API, NOTION_VERSION, _clean_id

ENV_PATH = BASE_DIR / ".env"
SOURCES = ["X", "Reddit", "V2EX", "WeChat", "GitHub", "Web", "Note", "Telegram"]
STATUSES = [("PASS", "green"), ("PARTIAL", "orange"), ("FALLBACK", "red"), ("AI_PENDING", "yellow")]
INPUT_TYPES = ["Link", "Text", "Image", "Voice", "PDF"]
FETCH_TIERS = ["Tier1-API", "Tier2-Chain", "Tier3-GeminiWeb", "Tier4-Fallback", "Direct-Multimodal", "Browser-Markdown"]


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
        "TelegramKey": {"rich_text": {}},
        "ClipKey": {"rich_text": {}},
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


def main(with_inbox: bool = False) -> None:
    token, parent = env("NOTION_TOKEN"), env("NOTION_PARENT_PAGE_ID")
    if not token or not parent:
        sys.exit("请先在 .env 填写 NOTION_TOKEN 和 NOTION_PARENT_PAGE_ID")
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
    cats = load_yaml_rules()
    names = [c["name"] for c in cats]
    with httpx.Client(headers=headers, timeout=30.0) as client:
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
    print("完成。之后在 Notion「分类配置」表里直接增删改分类即可，机器人 5 分钟内生效（发 /reload 立即生效）。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="创建 Notion 归档总表和分类配置表")
    parser.add_argument("--with-inbox", action="store_true", help="同时创建独立的归档收件箱")
    main(parser.parse_args().with_inbox)
