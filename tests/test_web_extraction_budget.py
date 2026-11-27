import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import router


class TestWebExtractionBudget(unittest.IsolatedAsyncioTestCase):
    url = "https://example.org/article"

    async def test_defuddle_gets_only_validated_html_and_final_url(self):
        final_url = "https://example.org/article/final"
        raw = "<article><h1>标题</h1><table><tr><td>表格</td></tr></table><pre>代码</pre></article>"
        response = httpx.Response(200, text=raw, headers={"content-type": "text/html; charset=utf-8"},
                                  request=httpx.Request("GET", final_url))
        parser_output = SimpleNamespace(stdout=json.dumps({"title": "标题", "content": "| 表格 |\n\n代码"}))
        with (patch.object(router, "_public_get", AsyncMock(return_value=(final_url, response))) as public_get,
              patch.object(router.subprocess, "run", return_value=parser_output) as node,
              patch.dict(os.environ, {"GEMINI_API_KEY": "example-secret"})):
            title, article = await router.fetch_via_defuddle(None, self.url)
        public_get.assert_awaited_once()
        self.assertEqual(public_get.await_args.args[1], self.url)
        self.assertEqual(json.loads(node.call_args.kwargs["input"]), {"url": final_url, "html": raw})
        self.assertNotIn("GEMINI_API_KEY", node.call_args.kwargs["env"])
        self.assertEqual((title, article), ("标题", "| 表格 |\n\n代码"))

    async def test_opt_in_runs_defuddle_first_and_unavailable_node_falls_back(self):
        article = "有效正文" * 100
        alternative = AsyncMock(return_value=("读取器标题", article))
        extraction = AsyncMock(side_effect=FileNotFoundError("Node unavailable"))
        with (patch.object(router, "_CHAIN_ORDER", [("Jina", alternative)]),
              patch.object(router, "fetch_via_defuddle", extraction),
              patch.dict(os.environ, {"DEFUDDLE_ENABLED": "1"})):
            title, text, channel, notes = await router.fetch_generic_chain(None, self.url)
        extraction.assert_awaited_once()
        alternative.assert_awaited_once()
        self.assertEqual((title, text, channel), ("读取器标题", article, "Jina"))
        self.assertEqual(notes, ["Defuddle失败：FileNotFoundError"])

        alternative.reset_mock()
        with (patch.object(router, "_CHAIN_ORDER", [("Jina", alternative)]),
              patch.object(router, "fetch_via_defuddle", AsyncMock(return_value=("本地标题", article))),
              patch.dict(os.environ, {"DEFUDDLE_ENABLED": "true"})):
            _, _, channel, notes = await router.fetch_generic_chain(None, self.url)
        self.assertEqual((channel, notes), ("Defuddle", []))
        alternative.assert_not_awaited()

    async def test_jina_applies_optional_budget_without_discarding_images(self):
        captured = []
        source = "证据 ![图表](https://example.org/figure.png)\n\n| 测试 | 结果 |"

        def handle(request):
            captured.append(request)
            return httpx.Response(200, text=f"Title: 样例\n\nMarkdown Content:\n{source}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.dict(os.environ, {"JINA_TOKEN_BUDGET": "6000"}):
                self.assertEqual(await router.fetch_via_jina(client, self.url), ("样例", source))
                self.assertEqual(await router.fetch_via_jina(client, self.url, preserve_layout=True), ("样例", source))
        generic, preserved = (item.headers for item in captured)
        self.assertEqual(generic["x-token-budget"], "6000")
        self.assertIn("nav", generic["x-remove-selector"])
        self.assertEqual(generic["x-retain-images"], "all")
        self.assertNotIn("x-remove-selector", preserved)
        self.assertNotIn("x-token-budget", preserved)

    async def test_jina_over_budget_response_falls_back_to_direct(self):
        direct = AsyncMock(return_value=("直连标题", "完整正文" * 100))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(422))) as client:
            with (patch.object(router, "_CHAIN_ORDER", [("Jina", router.fetch_via_jina), ("直连", None)]),
                  patch.object(router, "fetch_direct_html", direct),
                  patch.dict(os.environ, {"JINA_TOKEN_BUDGET": "6000", "DEFUDDLE_ENABLED": ""})):
                title, _, source, notes = await router.fetch_generic_chain(client, self.url)
        self.assertEqual((title, source, notes), ("直连标题", "直连", ["Jina失败：HTTP 422"]))

    async def test_jina_oversized_response_is_rejected(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"too much data")
        )) as client:
            with patch.object(router, "MAX_HTML_BYTES", 5), self.assertRaisesRegex(ValueError, "超过大小限制"):
                await router.fetch_via_jina(client, self.url)
