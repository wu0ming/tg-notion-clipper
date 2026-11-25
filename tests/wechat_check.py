"""
微信公众号抓取专项体检（不写入 Notion）：python tests/wechat_check.py [链接 ...]
- 链接来源：命令行参数 + tests/wechat_urls.txt（每行一个，# 开头为注释）；都没有时用内置公开样例
- 对每篇文章分别测：直连通道、Jina 通道、完整流水线（smart_fetch），对比标题/字数/图片数/耗时/状态
- 完整流水线的抓取结果保存到 tests/output/wechat_N.md，可直接打开检查排版与图片
"""
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from router import wechat_direct, wechat_jina, smart_fetch  # noqa: E402
from utils import normalize_url  # noqa: E402

SAMPLES = [
    "https://mp.weixin.qq.com/s/Y7dyRC7CJ09miHWU6LBzBA",   # wechat-article-to-markdown 项目的 E2E 默认样例
    "https://mp.weixin.qq.com/s/CxWTshkySZujG9kmkcMQEQ",   # jina-ai/reader #1183 中报告 Jina 抓不到的样例
]
OUT = ROOT / "tests" / "output"


def load_urls() -> list[str]:
    urls = [a for a in sys.argv[1:] if a.startswith("http")]
    f = ROOT / "tests" / "wechat_urls.txt"
    if f.exists():
        urls += [line.strip() for line in f.read_text(encoding="utf-8-sig").splitlines()
                 if line.strip().startswith("http")]
    return list(dict.fromkeys(urls)) or SAMPLES


def describe(result, seconds: float) -> str:
    if result is None:
        return f"未拿到正文（{seconds:.1f}s）"
    imgs = result.content.count("wsrv.nl/?url=")
    return (f"{result.status} 标题={result.title[:24]!r} 正文{len(result.content)}字 图片{imgs}张 "
            f"{seconds:.1f}s {result.note[:60]}")


async def timed(coro):
    start = time.perf_counter()
    try:
        result = await coro
        return result, time.perf_counter() - start, ""
    except Exception as exc:
        return None, time.perf_counter() - start, f"{type(exc).__name__}: {str(exc)[:100]}"


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, raw in enumerate(load_urls(), 1):
            url = normalize_url(raw)
            print(f"\n[{i}] {url}")
            for name, fn in (("直连", wechat_direct), ("Jina", wechat_jina)):
                result, sec, err = await timed(fn(client, url))
                line = f"异常 {err}（{sec:.1f}s）" if err else describe(result, sec)
                print(f"  {name:6}: {line}")
                rows.append((i, name, line))
            result, sec, err = await timed(smart_fetch(raw))
            line = f"异常 {err}" if err else describe(result, sec)
            print(f"  {'流水线':6}: {line}")
            rows.append((i, "流水线", line))
            if result:
                path = OUT / f"wechat_{i}.md"
                path.write_text(f"# {result.title}\n\n> {result.clean_url}\n> 状态：{result.status} {result.note}\n\n"
                                f"{result.content}\n", encoding="utf-8")
                print(f"  已保存：{path}")
    print("\n===== 微信抓取汇总 =====")
    for i, name, line in rows:
        print(f"#{i} | {name:6} | {line}")


if __name__ == "__main__":
    asyncio.run(main())
