"""
Gemini 分模型实测（不写 Notion）：python tests/gemini_check.py
对主模型、备用模型分别跑一次真实分类，打印完整结果或完整报错，便于判断是“模型繁忙”还是“Key/权限/Schema 问题”
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import env  # noqa: E402
import gemini_processor as gp  # noqa: E402

SAMPLE = "欧盟电池法规定 2027 年 2 月起动力电池需要电池护照，记得梳理供应链数据缺口。"


async def main() -> None:
    from category_config import get_categories
    cats, src = await get_categories(force=True)
    print(f"分类来源：{src}；共 {len(cats)} 个：" + "、".join(c["name"] for c in cats))
    key = env("GEMINI_API_KEY")
    print(f"Key 前缀：{key[:6]}…（长度 {len(key)}）")
    models = [env("GEMINI_MODEL") or gp.DEFAULT_MODEL, env("GEMINI_FALLBACK_MODEL") or gp.DEFAULT_FALLBACK]
    for model in dict.fromkeys(models):
        os.environ["GEMINI_MODEL"] = model
        os.environ["GEMINI_FALLBACK_MODEL"] = model
        try:
            r, rule, used = await gp.analyze_content("Note", "随手记", SAMPLE)
            print(f"\n[OK] {used}\n  分类：{r.category}\n  标题：{r.title}\n  标签：{r.tags}\n  摘要：{r.summary}")
        except Exception as exc:
            print(f"\n[FAIL] {model}\n  {exc}")


if __name__ == "__main__":
    asyncio.run(main())
