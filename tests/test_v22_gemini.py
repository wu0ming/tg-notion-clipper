import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.genai import types

import gemini_processor as gemini


URL = "https://example.com/article"
BODY = "原链接讨论了如何对混合文本与图像进行自动归档。它还记录了输入采集、文本提取、分类打标、写入知识库与异常补偿的几个主要流程。"
RULES = [
    {"name": "技术", "suggested_tags": ["Python", "OCR"]},
    {"name": "收件箱"},
]


def generated_response(*, text, url_status=None, retrieved_url=URL):
    candidate = types.Candidate(content=types.Content(parts=[types.Part(text=text)]))
    if url_status:
        candidate.url_context_metadata = types.UrlContextMetadata(url_metadata=[
            types.UrlMetadata(retrieved_url=retrieved_url, url_retrieval_status=url_status),
        ])
    return types.GenerateContentResponse(candidates=[candidate])


def mocked_client(responses):
    generate = AsyncMock(side_effect=responses)
    manager = AsyncMock()
    manager.__aenter__.return_value = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
    return manager, generate


class TestGeminiWeb(unittest.IsolatedAsyncioTestCase):
    async def test_url_context_recovers_original(self):
        text = json.dumps({"title": "资料", "content": BODY, "blocked": False, "complete": True})
        response = generated_response(text=text, url_status="URL_RETRIEVAL_STATUS_SUCCESS")
        manager, generate = mocked_client([response])
        with patch.object(gemini, "_client", return_value=manager), patch.object(gemini, "env", return_value="test"):
            title, content, status, _ = await gemini.recover_web(URL)
        self.assertEqual((title, content, status), ("资料", BODY, "PASS"))
        tools = generate.await_args.kwargs["config"].tools
        self.assertEqual(len(tools), 1)
        self.assertIsNotNone(tools[0].url_context)

    def test_partial_result_requires_matching_url_context(self):
        text = json.dumps({"title": "资料", "content": BODY, "blocked": False, "complete": False})
        unrelated = generated_response(text=text, url_status="URL_RETRIEVAL_STATUS_SUCCESS",
                                       retrieved_url="https://example.com/unrelated")
        matching = generated_response(text=text, url_status="URL_RETRIEVAL_STATUS_SUCCESS")
        self.assertEqual(gemini._web_result(unrelated, URL)[2], "FALLBACK")
        self.assertEqual(gemini._web_result(matching, URL)[2], "PARTIAL")

    def test_blocked_page_never_accepted(self):
        text = json.dumps({"title": "资料", "content": BODY, "blocked": False})
        response = generated_response(text=text, url_status="URL_RETRIEVAL_STATUS_PAYWALL")
        self.assertEqual(gemini._web_result(response, URL)[2], "FALLBACK")

    async def test_access_restriction_is_terminal(self):
        text = json.dumps({"title": "资料", "content": BODY, "blocked": False, "complete": True})
        blocked = generated_response(text=text, url_status="URL_RETRIEVAL_STATUS_UNSAFE")
        manager, generate = mocked_client([blocked])
        with patch.object(gemini, "_client", return_value=manager), \
                patch.object(gemini, "_models", return_value=["primary", "backup"]), \
                patch.object(gemini, "env", return_value="test"):
            self.assertEqual((await gemini.recover_web(URL))[2], "FALLBACK")
        self.assertEqual(generate.await_count, 1)


class TestGeminiAnalysis(unittest.IsolatedAsyncioTestCase):
    async def test_media_caption_and_tags_are_preserved(self):
        reply = {"title": "截图", "category": "技术", "summary": "摘要", "key_points": ["要点"],
                 "tags": ["python", "测试", "OCR"], "extracted_text": "图中的文字"}
        manager, generate = mocked_client([SimpleNamespace(text=json.dumps(reply))])
        with patch.object(gemini, "_client", return_value=manager), \
                patch.object(gemini, "get_categories", new=AsyncMock(return_value=(RULES, "test"))):
            result, rule, _ = await gemini.analyze_content(
                "Telegram", "", "", "图片批注", media_bytes=b"jpeg", media_mime="image/jpeg",
                input_type="Image", user_hashtags=["必看", "Python"],
            )
        self.assertEqual(result.extracted_text, "图中的文字")
        self.assertEqual(result.tags, ["必看", "Python", "OCR", "测试"])
        self.assertEqual(rule["name"], "技术")
        prompt, image = generate.await_args.kwargs["contents"]
        self.assertIn("图片批注", prompt)
        self.assertIn("建议标签", generate.await_args.kwargs["config"].system_instruction)
        self.assertEqual(image.inline_data.data, b"jpeg")

    async def test_invalid_category_uses_inbox(self):
        reply = {"title": "资料", "category": "无效分类", "summary": "摘要", "key_points": [], "tags": []}
        text = f"```json\n{json.dumps(reply)}\n```"
        manager, generate = mocked_client([SimpleNamespace(text=text)])
        with patch.object(gemini, "_client", return_value=manager), \
                patch.object(gemini, "get_categories", new=AsyncMock(return_value=(RULES, "test"))):
            result, rule, _ = await gemini.analyze_content("Web", "标题", "正文")
        self.assertEqual((result.category, rule["name"]), ("收件箱", "收件箱"))
        self.assertEqual(generate.await_count, 1)

    async def test_schema_rejection_retries_as_plain_json(self):
        error = gemini.api_errors.ClientError(400, {"error": {"message": "Unsupported schema"}})
        result_json = json.dumps({"title": "资料", "category": "技术", "summary": "摘要", "key_points": [], "tags": []})
        manager, generate = mocked_client([error, SimpleNamespace(text=result_json)])
        with patch.object(gemini, "_client", return_value=manager), \
                patch.object(gemini, "get_categories", new=AsyncMock(return_value=(RULES, "test"))):
            result, _, _ = await gemini.analyze_content("Web", "标题", "正文")
        self.assertEqual(result.category, "技术")
        self.assertIsNotNone(generate.await_args_list[0].kwargs["config"].response_schema)
        self.assertIsNone(generate.await_args_list[1].kwargs["config"].response_schema)

    async def test_missing_key_signals_fallback_without_external_call(self):
        with patch.object(gemini, "env", return_value=""), \
                patch.object(gemini, "get_categories", new=AsyncMock(return_value=(RULES, "test"))):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                await gemini.analyze_content("Web", "标题", "正文")
            self.assertEqual((await gemini.recover_web(URL))[2], "FALLBACK")

    async def test_default_model_budget_and_usage_by_intent(self):
        text = json.dumps({"title": "主题", "category": "技术", "summary": "摘要", "tags": [], "key_points": []})
        usage = SimpleNamespace(prompt_token_count=50, candidates_token_count=12, thoughts_token_count=3,
                                cached_content_token_count=4)
        response = SimpleNamespace(text=text, usage_metadata=usage)
        manager, generate = mocked_client([response, response])
        source = "原文开头" + "中间内容" * 6000 + "原文结论"
        with (patch.dict(os.environ, {"GEMINI_MODEL": "", "GEMINI_FALLBACK_MODEL": "", "GEMINI_COMPLEX_MODEL": ""}),
              patch.object(gemini, "_client", return_value=manager),
              patch.object(gemini, "get_categories", new=AsyncMock(return_value=(RULES, "test"))),
              self.assertLogs("clipper", level="INFO") as logs):
            await gemini.analyze_content("Web", "普通标题", source)
            await gemini.analyze_content("Web", "精读标题", source, "请精读这篇文章")

        for call, model, level, budget in zip(generate.await_args_list,
                                              (gemini.DEFAULT_MODEL, gemini.DEFAULT_FALLBACK),
                                              ("MINIMAL", "LOW"), (5500, 12000)):
            self.assertEqual(call.kwargs["model"], model)
            config = call.kwargs["config"]
            self.assertIsNone(config.temperature)
            self.assertEqual(config.thinking_config.thinking_level, level)
            self.assertIn("【分类规则】", config.system_instruction)
            self.assertNotIn("原文开头", config.system_instruction)
            body = call.kwargs["contents"].split("- 正文内容：\n", 1)[1].removesuffix("\n")
            self.assertEqual(len(body), budget)
            self.assertTrue(body.startswith("原文开头"))
            self.assertTrue(body.endswith("原文结论"))

        events = [json.loads(record.split("gemini_usage ", 1)[1]) for record in logs.output]
        self.assertEqual([event["prompt_tokens"] for event in events], [50, 50])
        self.assertEqual([event["thinking_tokens"] for event in events], [3, 3])
        self.assertEqual([event["cached_tokens"] for event in events], [4, 4])
        self.assertNotIn("精读标题", "\n".join(logs.output))
