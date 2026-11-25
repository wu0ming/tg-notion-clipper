import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import category_config
import notion_writer


class TestSuggestedTags(unittest.IsolatedAsyncioTestCase):
    async def test_notion_tag_formats_and_force_reload(self):
        def page(name, suggested):
            return {"properties": {
                "分类名": {"type": "title", "title": [{"plain_text": name}]},
                "启用": {"checkbox": True},
                "建议标签": suggested,
            }}

        config = category_config.parse_config_rows([
            page("A", {"type": "multi_select", "multi_select": [{"name": "AI Agent"}, {"name": "ESG"}]}),
            page("B", {"type": "rich_text", "rich_text": [{"plain_text": "#电池、AI Agent，#电池\n合规"}]}),
        ])
        self.assertEqual(config[0]["suggested_tags"], ["AI Agent", "ESG"])
        self.assertEqual(config[1]["suggested_tags"], ["电池", "AI Agent", "合规"])

        read = AsyncMock(side_effect=[config, config[1:]])
        with (patch.dict(os.environ, {"NOTION_DB_CONFIG": "config", "GLOBAL_SUGGESTED_TAGS": "AI Agent、#电池"}),
              patch.object(category_config, "_state", {"cats": None, "loaded_at": 0, "source": ""}),
              patch.object(category_config, "_fetch_from_notion", read)):
            self.assertEqual(category_config.global_suggested_tags(), ["AI Agent", "电池"])
            self.assertEqual((await category_config.get_categories())[0], config)
            self.assertEqual((await category_config.get_categories())[0], config)
            self.assertEqual((await category_config.get_categories(force=True))[0], config[1:])
        self.assertEqual(read.await_count, 2)


class TestNotionV22(unittest.IsolatedAsyncioTestCase):
    result = SimpleNamespace(title="标题", category="待整理", tags=["A", "B", "C", "D", "E"],
                             summary="摘要", key_points=["要点"], extracted_text="Hi，我收到了一条语音。")

    def setUp(self):
        env = patch.dict(os.environ, {"NOTION_DB_ARCHIVE": "archive", "NOTION_DB_INBOX": "inbox"})
        env.start()
        self.addCleanup(env.stop)
        for name in ("_ds_cache", "_schema_cache", "_write_locks"):
            patcher = patch.object(notion_writer, name, {})
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_attachment_and_ai_pending_in_separate_inbox(self):
        def handler(request):
            path = request.url.path
            if path.startswith("/v1/databases/"):
                return httpx.Response(200, json={"data_sources": [{"id": path.rsplit("/", 1)[-1]}]})
            if path in ("/v1/data_sources/inbox", "/v1/data_sources/archive"):
                props = {"标题": {"type": "title"}, "Status": {"type": "select", "select": {"options": []}},
                         "Tags": {"type": "multi_select"}, "InputType": {"type": "select"},
                         "FetchTier": {"type": "select"}, "TelegramKey": {"type": "rich_text"}}
                return httpx.Response(200, json={"properties": props})
            if path.endswith("/query"):
                self.assertEqual(json.loads(request.content)["filter"],
                                 {"property": "TelegramKey", "rich_text": {"equals": "1:2"}})
                return httpx.Response(200, json={"results": []})
            if path == "/v1/file_uploads":
                return httpx.Response(200, json={"id": "file-1"})
            if path == "/v1/file_uploads/file-1/send":
                self.assertIn("multipart/form-data; boundary=", request.headers["content-type"])
                self.assertIn(b"voice.ogg", request.content)
                self.assertIn(b"binary data", request.content)
                return httpx.Response(200, json={"status": "uploaded"})
            self.assertEqual(path, "/v1/pages")
            page = json.loads(request.content)
            self.assertEqual(page["parent"]["data_source_id"], "inbox")
            self.assertEqual(page["properties"]["Status"]["select"]["name"], "AI_PENDING")
            self.assertEqual(page["properties"]["InputType"]["select"]["name"], "Voice")
            self.assertEqual(len(page["properties"]["Tags"]["multi_select"]), 5)
            self.assertIn("标题", page["properties"])
            self.assertNotIn("Name", page["properties"])
            self.assertEqual(page["children"][-1]["file"]["file_upload"]["id"], "file-1")
            display = json.dumps(page["children"], ensure_ascii=False)
            self.assertIn("Hi，我收到了一条语音。", display)
            self.assertIn("完整失败记录", display)
            self.assertIn("file_id: opaque-ref", display)
            self.assertIn("原始配文", display)
            self.assertNotIn("should-not-store", display)
            return httpx.Response(200, json={"url": "https://notion.so/saved"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(notion_writer, "_get_client", return_value=client):
                page = await notion_writer.save_to_notion(
                    self.result, "Telegram", "", "AI_PENDING", "", "原始配文", "完整失败记录", inbox=True,
                    input_type="Voice", fetch_tier="Tier4-Fallback", attachment=(b"binary data", "audio/ogg", "voice.ogg"),
                    media_metadata={"chat_id": 1, "message_id": 2, "file_id": "opaque-ref", "token": "should-not-store"},
                )
        self.assertEqual(page, ("https://notion.so/saved", False))

    async def test_unconfigured_inbox_and_attachment_failure_preserve_raw_data(self):
        def handler(request):
            path = request.url.path
            if path == "/v1/databases/archive":
                return httpx.Response(200, json={"data_sources": [{"id": "archive"}]})
            if path == "/v1/data_sources/archive":
                return httpx.Response(200, json={"properties": {"Name": {"type": "title"}}})
            if path == "/v1/file_uploads":
                return httpx.Response(400, json={"message": "over workspace limit"})
            self.assertEqual(path, "/v1/pages")
            page = json.loads(request.content)
            self.assertEqual(page["parent"]["data_source_id"], "archive")
            self.assertEqual(set(page["properties"]), {"Name"})
            display = json.dumps(page["children"], ensure_ascii=False)
            for value in ("AI_PENDING", "Tier4-Fallback", "https://example.org/a?utm_source=test", "对应的 Telegram"):
                self.assertIn(value, display)
            return httpx.Response(200, json={"url": "https://notion.so/saved"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with (patch.object(notion_writer, "_get_client", return_value=client),
                  patch.dict(os.environ, {"NOTION_DB_INBOX": ""})):
                page = await notion_writer.save_to_notion(
                    self.result, "Telegram", "https://example.org/a", "AI_PENDING", "", inbox=True,
                    original_url="https://example.org/a?utm_source=test", input_type="Image", fetch_tier="Tier4-Fallback",
                    attachment=(b"image", "image/jpeg", "image.jpg"),
                )
        self.assertEqual(page, ("https://notion.so/saved", False))

    async def test_concurrent_same_url_dedupes_between_databases(self):
        pages = {"archive": [], "inbox": []}

        async def handler(request):
            path = request.url.path
            source_id = path.split("/")[3] if path.count("/") >= 3 else None
            if path.startswith("/v1/databases/"):
                return httpx.Response(200, json={"data_sources": [{"id": source_id}]})
            if path.endswith("/query"):
                return httpx.Response(200, json={"results": pages[source_id]})
            if path.startswith("/v1/data_sources/"):
                return httpx.Response(200, json={"properties": {"Name": {"type": "title"}, "URL": {"type": "url"}}})
            await asyncio.sleep(0.01)
            db = json.loads(request.content)["parent"]["data_source_id"]
            page = {"url": "https://notion.so/saved"}
            pages[db].append(page)
            return httpx.Response(200, json=page)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.object(notion_writer, "_get_client", return_value=client):
                results = await asyncio.gather(*[
                    notion_writer.save_to_notion(self.result, "Web", "https://example.org/a", "FALLBACK", "", inbox=inbox)
                    for inbox in (True, False)
                ])
        self.assertEqual(sum(map(len, pages.values())), 1)
        self.assertEqual({dup for _, dup in results}, {False, True})
