import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import app
import notion_writer
from utils import normalize_url


class TestTelegramDedup(unittest.IsolatedAsyncioTestCase):
    url = "https://example.org/article"
    page = "https://notion.so/p/article"
    result = app.AnalysisResult(title="示例", category="待整理", tags=[], summary="摘要", key_points=[])

    def test_video_tracking_does_not_discard_timestamps_or_parts(self):
        self.assertEqual(normalize_url("https://youtu.be/sample?si=tracking&t=12#t=20"),
                         "https://youtu.be/sample?t=12#t=20")
        self.assertEqual(normalize_url("https://www.bilibili.com/video/BV123/?spm=tracking&p=2&t=45"),
                         "https://www.bilibili.com/video/BV123?p=2&t=45")
        self.assertEqual(normalize_url("https://example.org/?si=data"), "https://example.org/?si=data")

    def fetched(self):
        return SimpleNamespace(source_type="Web", clean_url=self.url, title="示例", content="正文",
                               status="PASS", note="", channel="mock", fetch_tier="mock")

    async def test_concurrent_exact_links_fetch_and_analyze_once(self):
        archive = {}

        async def find(url):
            await asyncio.sleep(0)
            return archive.get(url)

        async def save(result, source, url, *args, **kwargs):
            archive[url] = self.page
            return self.page, False

        with (patch.object(app, "_find_duplicate", side_effect=find),
              patch.object(app, "smart_fetch", AsyncMock(return_value=self.fetched())) as fetch,
              patch.object(app, "analyze_content", AsyncMock(return_value=(self.result, {}, "mock"))) as analyze,
              patch.object(app, "save_to_notion", side_effect=save)):
            receipts = await asyncio.gather(*(
                app._archive_message(f"{self.url}?utm_source={i}", [], {"message_id": i})
                for i in range(1, 4)
            ))

        fetch.assert_awaited_once()
        analyze.assert_awaited_once()
        self.assertEqual(sum("已归档过" in receipt for receipt, _ in receipts), 2)

    async def test_new_note_or_forwarded_evidence_skips_url_only_duplicate_filter(self):
        with (patch.object(app, "_find_duplicate", AsyncMock(return_value=self.page)) as find,
              patch.object(app, "smart_fetch", AsyncMock(return_value=self.fetched())),
              patch.object(app, "analyze_content", AsyncMock(return_value=(self.result, {}, "mock"))),
              patch.object(app, "save_to_notion", AsyncMock(return_value=(self.page, False))) as save):
            await app._archive_message(f"{self.url}\n批注：新的研究角度", [], {"message_id": 1})
            await app._archive_message(f"{self.url}\n转发的正文", [],
                                       {"message_id": 2, "forward_origin": {"type": "hidden_user"}})

        find.assert_not_called()
        original, forward = save.call_args_list
        self.assertEqual(original.args[5], "新的研究角度")
        self.assertEqual(forward.kwargs["forwarded_text"], f"{self.url}\n转发的正文")
        self.assertTrue(all(not call.kwargs["deduplicate_by_url"] for call in save.call_args_list))

    async def test_notion_preserves_original_url_and_deduplicates_message_replay(self):
        pages = {}
        schema = {"Name": {"type": "title"}, "TelegramKey": {"type": "rich_text"}, "URL": {"type": "url"}}

        def handle(request):
            path = request.url.path
            if path == "/v1/databases/archive":
                return httpx.Response(200, json={"data_sources": [{"id": "archive"}]})
            if path == "/v1/data_sources/archive":
                return httpx.Response(200, json={"properties": schema})
            body = json.loads(request.content)
            if path.endswith("/query"):
                self.assertEqual(body["filter"]["property"], "TelegramKey")
                key = body["filter"]["rich_text"]["equals"]
                return httpx.Response(200, json={"results": [pages[key]] if key in pages else []})
            self.assertEqual(path, "/v1/pages")
            props = body["properties"]
            self.assertEqual(props["URL"]["url"], self.url)
            key = props["TelegramKey"]["rich_text"][0]["text"]["content"]
            pages[key] = {"url": f"{self.page}/{key}"}
            return httpx.Response(200, json=pages[key])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with (patch.dict(os.environ, {"NOTION_DB_ARCHIVE": "archive", "NOTION_DB_INBOX": ""}),
                  patch.object(notion_writer, "_ds_cache", {}),
                  patch.object(notion_writer, "_schema_cache", {}),
                  patch.object(notion_writer, "_get_client", return_value=client)):
                flags = []
                for i in (1, 2, 1):
                    _, duplicate = await notion_writer.save_to_notion(
                        self.result, "Web", self.url, "PASS", "来源正文", user_note=f"独立批注 {i}",
                        telegram_message={"chat_id": 123, "message_id": i}, deduplicate_by_url=False,
                    )
                    flags.append(duplicate)
        self.assertEqual(flags, [False, False, True])
        self.assertEqual(len(pages), 2)
