"""
Description: 基于 Gemini Structured Outputs 的分类与差异化打标
Changelog:
- v1.0 (2026-11): 方案原始版本（gemini-2.5-flash，同步调用）
- v1.1 (2026-11): 模型可配置 + 主备切换；异步客户端；category 枚举约束；FALLBACK 时禁止臆造正文
- v1.2 (2026-11): 分类改为从 Notion「分类配置」表动态读取（category_config.py）；默认模型 gemini-3.8-flash（付费层），
  备用 gemini-3.5-flash-lite；503/429 等临时错误先等 3 秒重试一次，再切换备用模型
- v2.0 (2026-11): 增加 60 秒调用超时（防止 Gemini 卡住时 Cloud Run 120 秒强制终止而用户收不到回执）；
  超时不重试，直接切换备用模型
"""
import asyncio
import enum
from functools import lru_cache

import yaml
from pydantic import BaseModel, Field, create_model
from google import genai
from google.genai import types

from config import env
from category_config import get_categories

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACK = "gemini-3.5-flash-lite"
TRANSIENT_HINTS = ("503", "429", "unavailable", "resource_exhausted", "high demand", "overloaded")
GEMINI_TIMEOUT = 60  # 秒，Cloud Run 请求超时 120 秒，留 60 秒余量给抓取和写 Notion


class AnalysisResult(BaseModel):
    title: str
    category: str
    tags: list[str]
    summary: str
    key_points: list[str]


@lru_cache(maxsize=16)
def response_model(names: tuple[str, ...]) -> type[BaseModel]:
    """按当前启用的分类动态生成带枚举约束的输出 Schema，保证 category 必然命中其中之一"""
    CategoryEnum = enum.Enum("CategoryEnum", {f"C{i}": n for i, n in enumerate(names)}, type=str)
    return create_model(
        "ClassificationResult",
        __module__=__name__,
        title=(str, Field(description="清晰规范的中文标题，不超过40字；纯文本随手记则生成15字以内概括标题")),
        category=(CategoryEnum, Field(description="必须是分类规则中的某一个分类名")),
        tags=(list[str], Field(description="严格遵循命中分类的标签规则生成的2到4个标签")),
        summary=(str, Field(description="遵循命中分类的提取重点提炼的3到5句高密度中文摘要")),
        key_points=(list[str], Field(description="3到6条结构化核心要点，每条简洁有力")),
    )


def rules_text(categories: list[dict]) -> str:
    rows = [{"分类名": c["name"], "描述": c.get("description", ""), "提取重点": c.get("extraction_focus", ""),
             "标签规则": c.get("tagging_rule", "")} for c in categories]
    return yaml.dump(rows, allow_unicode=True, sort_keys=False)


def build_prompt(categories: list[dict], source_type: str, title: str, content: str, user_note: str,
                 status: str) -> str:
    caution = ""
    if status == "FALLBACK":
        caution = ("\n注意：本条正文抓取失败，只有链接/标题等元数据。请仅依据可见信息与用户批注分类，"
                   "摘要中明确写“原文未抓取成功”，不要臆造原文内容。\n")
    return f"""你是用户的个人知识管理助手。请阅读以下素材，先判断它最匹配哪个分类（只能从下列分类名中选一个），再严格按照该分类的「提取重点」和「标签规则」生成标题、摘要、要点和标签。所有输出使用简体中文（专有名词、法规缩写保留原文）。
{caution}
【分类规则】
{rules_text(categories)}
【输入素材】
- 来源平台：{source_type}
- 抓取状态：{status}
- 原始标题：{title or "无"}
- 用户附带批注（高优先级参考，可直接决定分类）：{user_note or "无"}
- 正文内容（已截断前15000字）：
{(content or "")[:15000]}
"""


def match_category(categories: list[dict], name: str) -> dict:
    hit = next((c for c in categories if c["name"] == name), None)
    if not hit and name:
        hit = next((c for c in categories if c["name"] in name or name in c["name"]), None)
    return hit or categories[0]  # 排序第一的分类兼作兜底


def _is_transient(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(h in text for h in TRANSIENT_HINTS)


async def analyze_content(source_type: str, title: str, content: str, user_note: str = "",
                          status: str = "PASS") -> tuple[AnalysisResult, dict, str]:
    """返回 (分析结果, 命中的分类, 实际使用的模型名)"""
    categories, _source = await get_categories()
    schema = response_model(tuple(c["name"] for c in categories))
    client = genai.Client(api_key=env("GEMINI_API_KEY"))
    prompt = build_prompt(categories, source_type, title, content, user_note, status)
    models = list(dict.fromkeys(m for m in (env("GEMINI_MODEL") or DEFAULT_MODEL,
                                            env("GEMINI_FALLBACK_MODEL") or DEFAULT_FALLBACK) if m))
    errors = []
    for model in models:
        for attempt in range(2):
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model, contents=prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json",
                                                           response_schema=schema, temperature=0.2)),
                    timeout=GEMINI_TIMEOUT)
                raw = schema.model_validate_json(response.text)
                data = raw.model_dump()
                data["category"] = getattr(raw.category, "value", raw.category)
                result = AnalysisResult(**data)
                rule = match_category(categories, result.category)
                result.category = rule["name"]
                return result, rule, model
            except asyncio.TimeoutError:
                errors.append(f"{model}: 超时（{GEMINI_TIMEOUT}秒），切换模型")
                break  # 超时不重试，直接换下一个模型
            except Exception as exc:
                errors.append(f"{model}: {type(exc).__name__} {str(exc)[:120]}")
                if attempt == 0 and _is_transient(exc):
                    await asyncio.sleep(3)
                    continue
                break
    raise RuntimeError("Gemini 调用失败：" + " | ".join(errors))
