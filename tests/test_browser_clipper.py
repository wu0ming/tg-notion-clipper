import asyncio
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_clipper as clipper
import notion_writer
from notion_writer import _lookup_filter


class TestBrowserClip(unittest.IsolatedAsyncioTestCase):
    def test_pairing_is_scoped_signed_and_expiring(self):
        with patch.dict(os.environ, {"ALLOWED_USER_ID": "123", "BROWSER_CLIP_SECRET": "test-signing-key"}), \
                patch.object(clipper.time, "time", return_value=1800000000) as clock:
            self.assertIsNone(clipper.issue_clip_token(456))
            token = clipper.issue_clip_token(123)
            self.assertTrue(clipper.authorized_clip(f"Bearer {token}"))
            suffix = "a" if token[-1] != "a" else "b"
            self.assertFalse(clipper.authorized_clip(f"Bearer {token[:-1]}{suffix}"))
            self.assertFalse(clipper.authorized_clip("Bearer test-signing-key"))
            clock.return_value += clipper.TOKEN_LIFETIME
            self.assertFalse(clipper.authorized_clip(f"Bearer {token}"))

    def test_clip_accepts_http_sources_without_credentials(self):
        for url in ("javascript:alert(1)", "https://user:password@example.com", "file:///tmp/document"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                clipper.BrowserClip(title="test", url=url, markdown="text", mode="page")

    async def test_archives_exact_selection_source_and_note_in_the_child_page(self):
        clip = clipper.BrowserClip(title="样例", url="https://example.org/article?a=1#part", mode="selection",
                                  markdown="只选中 **这一段**。", annotation="这段适用于后续工作。")
        result = SimpleNamespace(title="样例", summary="摘要", category="测试", tags=["参考"], key_points=[])
        with patch.object(clipper, "find_existing", AsyncMock(return_value=None)), \
                patch.object(clipper, "analyze_content", AsyncMock(return_value=(result, {}, "model"))) as analyze, \
                patch.object(clipper, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/1", False))) as save, \
                patch.object(clipper, "ensure_markdown_child", AsyncMock()) as child:
            response = await clipper.archive_browser_clip(clip)
        self.assertEqual(response, {"ok": True, "url": "https://notion.so/p/1", "duplicate": False, "status": "PASS"})
        self.assertEqual(analyze.call_args.args[2:4], (clip.markdown, clip.annotation))
        self.assertEqual(save.call_args.args[2], clip.url)
        self.assertNotIn("attachment", save.call_args.kwargs)
        child.assert_awaited_once()
        parent_url, markdown = child.call_args.args
        self.assertEqual(parent_url, response["url"])
        self.assertIn(clip.url, markdown)
        self.assertIn(clip.annotation, markdown)
        self.assertIn(clip.markdown, markdown)

    async def test_duplicate_reuses_notion_page_before_classification(self):
        clip = clipper.BrowserClip(title="题目", url="https://example.org/article", mode="page", markdown="一篇文章")
        with patch.object(clipper, "find_existing", AsyncMock(return_value="https://notion.so/p/existing")) as lookup, \
                patch.object(clipper, "analyze_content", AsyncMock()) as analyze, \
                patch.object(clipper, "ensure_markdown_child", AsyncMock()) as child:
            response = await clipper.archive_browser_clip(clip)
        self.assertTrue(response["duplicate"])
        self.assertEqual(len(lookup.call_args.kwargs["clip_key"]), 64)
        analyze.assert_not_called()
        child.assert_awaited_once_with(response["url"], clipper._markdown_document(clip))

    def test_browser_deduplication_is_independent_of_telegram_url_deduplication(self):
        schema = {"URL": {"type": "url"}, "ClipKey": {"type": "rich_text"}}
        url = "https://example.org/article"
        self.assertEqual(_lookup_filter(schema, url, clip_key="key"), {"property": "ClipKey", "rich_text": {"equals": "key"}})
        self.assertIn({"property": "ClipKey", "rich_text": {"is_empty": True}}, _lookup_filter(schema, url)["and"])

    async def test_concurrent_requests_create_one_native_markdown_child(self):
        parent = "423bbbafe8498174a84ae7db116cb741"
        markdown = "## 原文\n\n[链接](https://example.org/)和 **加粗**"
        created = []

        async def handler(request):
            if request.url.path == f"/v1/blocks/{parent}/children":
                await asyncio.sleep(0)
                return httpx.Response(200, json={"results": created})
            self.assertEqual(request.url.path, "/v1/pages")
            body = json.loads(request.content)
            self.assertEqual(body["parent"], {"type": "page_id", "page_id": parent})
            self.assertEqual(body["markdown"], markdown)
            self.assertNotIn("children", body)
            created.append({"type": "child_page", "id": "child", "child_page": {"title": "剪藏原文"}})
            return httpx.Response(200, json={"id": "child"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(notion_writer, "_get_client", return_value=client):
                await asyncio.gather(*(notion_writer.ensure_markdown_child(
                    f"https://app.notion.com/p/{parent}", markdown) for _ in range(2)))
        self.assertEqual(len(created), 1)

    async def test_retries_notion_image_parse_error_with_standalone_images(self):
        parent = "423bbbafe8498174a84ae7db116cb741"
        markdown = ("[![架构图](https://example.org/p.png)](https://example.org/project)\n\n"
                    "文字 ![另一个](https://example.org/q.png) 下一段\n\n"
                    "```markdown\n示例 ![代码](https://example.org/code.png)\n```\n")
        writes = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"results": []})
            body = json.loads(request.content)
            writes.append(body["markdown"])
            if len(writes) == 1:
                return httpx.Response(400, json={"message": "Images must be standalone blocks"})
            self.assertEqual(body["markdown_version"], "v2")
            return httpx.Response(200, json={"id": "child"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(notion_writer, "_get_client", return_value=client):
                await notion_writer.ensure_markdown_child(f"https://app.notion.com/p/{parent}", markdown)
        self.assertEqual(writes[0], markdown)
        self.assertIn("![架构图](https://example.org/p.png)\n\n[架构图](https://example.org/project)", writes[1])
        self.assertIn("文字 \n\n![另一个](https://example.org/q.png)\n\n 下一段", writes[1])
        self.assertIn("```markdown\n示例 ![代码](https://example.org/code.png)\n```", writes[1])
