"""
本地端到端测试（真实写入 Notion、真实给你的 Telegram 发回执）：python tests/e2e_local.py
模拟你本人（ALLOWED_USER_ID）发来的 6 条消息，逐条走完整流水线：
  app.process_update → 抓取 → Gemini 分类 → 写入 Notion 归档总表 → Telegram 回执
与线上唯一的区别：消息不是 Telegram 推送进来的，而是脚本直接构造（因此不需要 Webhook、不需要部署）
每条结果追加写入 tests/output/e2e_<时间>.json，跑完自动把汇总表写进 docs/方案与进度.md 的「16. 本地端到端测试记录」
"""
import asyncio
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from config import env, allowed_user_ids  # noqa: E402
import app  # noqa: E402

DOC = ROOT / "docs" / "方案与进度.md"
OUT = ROOT / "tests" / "output" / f"e2e_{datetime.now():%Y%m%d_%H%M}.json"
WECHAT = "https://mp.weixin.qq.com/s/Y7dyRC7CJ09miHWU6LBzBA"


async def v2ex_url() -> str:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            hot = (await c.get("https://www.v2ex.com/api/topics/hot.json",
                               headers={"User-Agent": "tg-notion-clipper e2e"})).json()
            return f"https://www.v2ex.com/t/{hot[0]['id']}"
    except Exception:
        return "https://www.v2ex.com/t/1000000"


def cases(v2: str) -> list[tuple[str, str, str]]:
    stamp = datetime.now().strftime("%m%d%H%M")
    return [
        ("随手记", f"【E2E测试 {stamp}】欧盟电池法 2027-02-18 起动力电池需带电池护照，记得梳理供应商碳足迹数据缺口。", "新建"),
        ("X 推文", "https://x.com/jack/status/20 E2E测试：推特第一条推文", "新建"),
        ("V2EX", f"{v2} E2E测试", "新建"),
        ("微信公众号", f"{WECHAT}?utm_source=e2e&scene=21 E2E测试：带追踪参数", "新建"),
        ("通用网页", "https://github.com/FxEmbed/FxEmbed", "新建"),
        ("重复链接", WECHAT, "去重"),
    ]


def doc_append(rows: list[dict], started: str, finished: str) -> None:
    if not DOC.is_file():
        return
    lines = [f"\n### {started[:16].replace('T', ' ')} 本地端到端测试（{sum(r['pass'] for r in rows)}/{len(rows)} 通过）\n",
             "| # | 场景 | 结果 | 分类 | 抓取状态 | 耗时 | 说明 |", "| --: | :-- | :-- | :-- | :-- | --: | :-- |"]
    for r in rows:
        mark = "🟢 通过" if r["pass"] else "🔴 未通过"
        lines.append(f"| {r['i']} | {r['case']} | {mark} | {r.get('category') or '—'} | {r.get('status') or '—'} | "
                     f"{r['sec']}s | {r['detail'][:80].replace('|', '/')} |")
    lines.append(f"\n> 开始 {started}，结束 {finished}；明细：`{OUT.relative_to(ROOT).as_posix()}`\n")
    text = DOC.read_text(encoding="utf-8")
    head = "## 16. 本地端到端测试记录"
    if head not in text:
        text = text.rstrip("\n") + f"\n\n---\n\n{head}\n\n每次运行 `python tests\\e2e_local.py` 自动追加一节。\n"
    DOC.write_text(text.rstrip("\n") + "\n" + "\n".join(lines), encoding="utf-8")


async def main() -> None:
    uids = sorted(allowed_user_ids())
    if not uids:
        sys.exit("ALLOWED_USER_ID 未配置")
    uid = uids[0]
    sent: list[dict] = []
    real_tg = app.tg_call

    async def capture(method: str, payload: dict) -> dict | None:
        if method in {"editMessageText", "sendMessage"} and not payload["text"].startswith("⏳"):
            sent.append(payload)
        return await real_tg(method, payload)

    app.tg_call = capture
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    v2 = await v2ex_url()
    rows = []
    print(f"开始 {started}  用户 {uid}  模型 {env('GEMINI_MODEL')}")
    for i, (name, text, expect) in enumerate(cases(v2), 1):
        before = len(sent)
        upd = {"update_id": int(time.time() * 1000) + i,
               "message": {"message_id": 900000 + i, "date": int(time.time()), "text": text,
                           "chat": {"id": uid, "type": "private"}, "from": {"id": uid, "is_bot": False}}}
        t0 = time.perf_counter()
        await app.process_update(upd)
        sec = round(time.perf_counter() - t0, 1)
        reply = sent[before]["text"] if len(sent) > before else ""
        cat = (re.search(r"已归档至【(.+?)】", reply) or [None, None])[1]
        st = (re.search(r"（\S+ · (PASS|PARTIAL|FALLBACK)）", reply) or [None, None])[1]
        url = (re.search(r"https://www\.notion\.so/\S+|https://app\.notion\.com/\S+", reply) or [""])[0]
        if expect == "去重":
            ok = "已归档过" in reply
        else:
            ok = reply.startswith(("✅", "🟠", "🔴")) and "已归档至" in reply and bool(url)
        detail = reply.replace("\n", " / ")[:300] if reply else "没有收到回执"
        rows.append({"i": i, "case": name, "input": text, "expect": expect, "pass": ok, "category": cat,
                     "status": st, "notion_url": url, "sec": sec, "detail": detail})
        print(f"[{i}] {'PASS' if ok else 'FAIL'} {name:6} {sec:>5}s  {detail[:150]}")
        OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    finished = datetime.now().astimezone().isoformat(timespec="seconds")
    doc_append(rows, started, finished)
    print(f"\n===== 汇总 =====\n通过 {sum(r['pass'] for r in rows)}/{len(rows)}\n明细：{OUT}")
    if DOC.is_file():
        print(f"已写入文档：{DOC}")


if __name__ == "__main__":
    asyncio.run(main())
