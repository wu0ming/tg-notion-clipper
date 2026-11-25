"""
联网体检（不写入任何数据）：python tests/live_check.py [额外测试链接 ...]
1) 抓取通道：FxTwitter / V2EX / Reddit / Jina 通用网页（以及你传入的微信公众号等链接）
2) 若 .env 已填密钥：Telegram getMe、Gemini 模型可用性、Notion Token 与 4 个数据库可达性
可选：python tests/live_check.py --e2e "一条测试随手记"   → 走完整流水线并真实写入 Notion
注意：在中国大陆本机运行需先设置代理，例如  set HTTPS_PROXY=http://127.0.0.1:7890  （httpx 会自动读取）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from config import env  # noqa: E402
from router import (smart_fetch, DEFAULT_UA, fetch_via_tavily, fetch_via_firecrawl,  # noqa: E402
                    fetch_via_zenrows, fetch_via_jina)

rows: list[tuple[str, str, str]] = []


def record(name: str, ok: bool | None, detail: str) -> None:
    mark = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
    rows.append((name, mark, detail))
    print(f"[{mark}] {name}: {detail}")


async def pick_samples(client: httpx.AsyncClient) -> dict[str, str]:
    samples = {"X": "https://x.com/jack/status/20", "Web": "https://github.com/FxEmbed/FxEmbed"}
    try:
        latest = (await client.get("https://www.v2ex.com/api/topics/hot.json",
                                   headers={"User-Agent": DEFAULT_UA}, timeout=15)).json()
        samples["V2EX"] = f"https://www.v2ex.com/t/{latest[0]['id']}"
    except Exception as exc:
        record("V2EX 样本获取", False, str(exc)[:120])
    samples["Reddit"] = "https://www.reddit.com/r/Python/comments/1ex5kvl/"  # 若失效会走 Jina 降级，仍可验证通道
    return samples


async def check_fetchers(extra: list[str]) -> None:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        samples = await pick_samples(client)
        test_url = "https://github.com/FxEmbed/FxEmbed"
        for name, key_name, fn in (
            ("Tavily", "TAVILY_API_KEY", fetch_via_tavily),
            ("Firecrawl", "FIRECRAWL_API_KEY", fetch_via_firecrawl),
            ("ZenRows", "ZENROWS_API_KEY", fetch_via_zenrows),
            ("Jina", "JINA_API_KEY", fetch_via_jina),
        ):
            if not env(key_name):
                record(f"渠道-{name}", None, f"{key_name} 未填")
                continue
            try:
                title, text = await fn(client, test_url)
                record(f"渠道-{name}", len(text) >= 200, f"标题={title[:24]!r} 正文{len(text)}字")
            except Exception as exc:
                record(f"渠道-{name}", False, f"{type(exc).__name__}: {str(exc)[:120]}")
    for i, url in enumerate(extra):
        samples[f"自定义{i + 1}"] = url
    for name, url in samples.items():
        r = await smart_fetch(url)
        ok = r.status in ("PASS", "PARTIAL") and len(r.content) > 50
        record(f"抓取-{name}", ok,
               f"{r.source_type}/{r.status} 渠道={r.channel or '-'} 标题={r.title[:26]!r} 正文{len(r.content)}字 {r.note[:60]}")


async def check_credentials() -> None:
    async with httpx.AsyncClient(timeout=20) as client:
        if env("TELEGRAM_BOT_TOKEN"):
            me = (await client.get(f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/getMe")).json()
            record("Telegram Token", me.get("ok"), f"@{me.get('result', {}).get('username')}" if me.get("ok") else str(me)[:120])
        else:
            record("Telegram Token", None, ".env 未填")

        if env("GEMINI_API_KEY"):
            for key, default in (("GEMINI_MODEL", "gemini-3.8-flash"), ("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")):
                model = env(key) or default
                resp = await client.get(f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
                                        params={"key": env("GEMINI_API_KEY")})
                record(f"Gemini {key}", resp.status_code == 200, f"{model} → HTTP {resp.status_code}")
        else:
            record("Gemini Key", None, ".env 未填")

        if env("NOTION_TOKEN"):
            from notion_writer import resolve_data_source_id, _headers, NOTION_API
            me = await client.get(f"{NOTION_API}/users/me", headers=_headers())
            record("Notion Token", me.status_code == 200, f"HTTP {me.status_code}")
            need_map = {
                "NOTION_DB_ARCHIVE": {"Name": "title", "Category": "select", "Tags": "multi_select", "Source": "select",
                                      "URL": "url", "Summary": "rich_text", "Status": "select", "Created": "date"},
                "NOTION_DB_CONFIG": {"分类名": "title", "描述": "rich_text", "提取重点": "rich_text",
                                     "标签池": "rich_text", "启用": "checkbox", "排序": "number"},
            }
            for key, need in need_map.items():
                if not env(key):
                    record(f"Notion {key}", None, "未配置（先运行 python setup_notion.py）")
                    continue
                try:
                    ds = await resolve_data_source_id(client, env(key))
                    props = (await client.get(f"{NOTION_API}/data_sources/{ds}", headers=_headers())).json().get("properties", {})
                    bad = [f"{k}({v})" for k, v in need.items() if (props.get(k) or {}).get("type") != v]
                    record(f"Notion {key}", not bad, "字段齐全" if not bad else "缺少/类型不符：" + ", ".join(bad))
                except Exception as exc:
                    record(f"Notion {key}", False, str(exc)[:150])
            from category_config import get_categories
            cats, src = await get_categories(force=True)
            record("分类配置读取", src.startswith("Notion"), f"{len(cats)} 个启用分类，来源：{src}：" +
                   "、".join(c["name"] for c in cats))
        else:
            record("Notion Token", None, ".env 未填")

        if env("GEMINI_API_KEY"):
            from gemini_processor import analyze_content
            try:
                r, rule, model = await analyze_content(
                    "Note", "随手记", "欧盟电池法规定 2027 年 2 月起动力电池需要电池护照，记得梳理供应链数据缺口。")
                record("Gemini 分类实测", True, f"{model} → 【{r.category}】{r.title} 标签={r.tags}")
            except Exception as exc:
                record("Gemini 分类实测", False, str(exc)[:200])


async def e2e(text: str) -> None:
    from app import archive_text
    try:
        record("端到端写入", True, (await archive_text(text, None)).replace("\n", " | ")[:300])
    except Exception as exc:
        record("端到端写入", False, f"{type(exc).__name__}: {str(exc)[:200]}")


async def main() -> None:
    args = sys.argv[1:]
    if "--e2e" in args:
        i = args.index("--e2e")
        await e2e(args[i + 1] if i + 1 < len(args) else "测试：部署体检随手记")
        return
    await check_fetchers(args)
    await check_credentials()
    print("\n===== 汇总 =====")
    for name, mark, detail in rows:
        print(f"{mark:5} | {name:22} | {detail}")


if __name__ == "__main__":
    asyncio.run(main())
