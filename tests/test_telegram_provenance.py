import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import app
import notion_writer
from telegram_source import forward_source


class TestTelegramProvenance(unittest.IsolatedAsyncioTestCase):
    author = {"type": "hidden_user", "sender_user_name": "转发作者", "date": 1770000000}
    analysis = app.AnalysisResult(title="项目", category="技术工具与开源", tags=["工具"],
                                  summary="摘要", key_points=[])
    saved_url = "https://notion.so/p/archive"

    def test_metadata_uses_original_author_and_only_public_channel_links(self):
        for origin, expected in (
            ({"type": "user", "sender_user": {"id": 210, "first_name": "原作者"}},
             {"来源类型": "用户", "作者": "原作者"}),
            (self.author, {"来源类型": "隐藏作者", "作者": "转发作者", "原消息时间戳": 1770000000}),
            ({"type": "chat", "sender_chat": {"title": "私聊"}, "author_signature": "署名"},
             {"来源类型": "聊天", "作者": "署名", "聊天或频道": "私聊"}),
            ({"type": "channel", "chat": {"title": "频道", "username": "public_channel"}, "message_id": 31},
             {"来源类型": "频道", "聊天或频道": "频道", "原消息编号": 31,
              "链接": "https://t.me/public_channel/31"}),
        ):
            with self.subTest(origin=origin):
                self.assertEqual(forward_source({"forward_origin": origin, "from": {"first_name": "提交者"}}),
                                 expected)
        self.assertEqual(forward_source({"forward_date": 31, "forward_sender_name": "旧版隐藏作者"}),
                         {"来源类型": "隐藏作者", "作者": "旧版隐藏作者", "原消息时间戳": 31})
        self.assertEqual(forward_source({"from": {"first_name": "提交者"}}), {})

    async def test_forwarded_command_goes_through_webhook_without_running_it(self):
        payload = {"message": {"message_id": 4, "from": {"id": 123},
                               "chat": {"id": 123, "type": "private"}, "forward_origin": self.author,
                               "text": "/clipkey\n批注：原作者要求 #原始标签"}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app), base_url="https://bot.test") as client:
            with (patch.dict(os.environ, {"ALLOWED_USER_ID": "123", "WEBHOOK_SECRET": "mock-webhook"}),
                  patch.object(app, "tg_call", AsyncMock(return_value={"ok": True})),
                  patch.object(app, "issue_clip_token", Mock()) as issue_token,
                  patch.object(app, "analyze_content", AsyncMock(return_value=(self.analysis, {}, "test"))) as analyze,
                  patch.object(app, "save_to_notion", AsyncMock(return_value=(self.saved_url, False))) as save):
                response = await client.post("/webhook", json=payload,
                                             headers={"X-Telegram-Bot-Api-Secret-Token": "mock-webhook"})

        self.assertEqual(response.json(), {"ok": True})
        issue_token.assert_not_called()
        analyze.assert_awaited_once()
        self.assertIn(payload["message"]["text"], analyze.call_args.args[2])
        self.assertEqual(analyze.call_args.args[3], "")
        self.assertEqual(analyze.call_args.kwargs["user_hashtags"], [])
        self.assertEqual(save.call_args.args[5], "")
        self.assertEqual(save.call_args.kwargs["forwarded_text"], payload["message"]["text"])

    async def test_forward_with_caption_retains_link_and_media_but_no_personal_note(self):
        url = "https://example.org/article"
        caption = f"批注：作者的 caption #作者标签\n{url}"
        message = {"message_id": 5, "chat": {"id": 123}, "media_group_id": "group-1",
                   "photo": [{"file_id": "file-5"}], "forward_origin": self.author}
        with (patch.object(app, "_get_tg_client"),
              patch.object(app, "download_media", AsyncMock(return_value=b"media")),
              patch.object(app, "analyze_content", AsyncMock(return_value=(self.analysis, {}, "test"))) as analyze,
              patch.object(app, "save_to_notion", AsyncMock(return_value=(self.saved_url, False))) as save):
            await app._archive_message(caption, [], message, app.extract_media(message))

        self.assertEqual(analyze.await_count, 1)
        self.assertEqual(analyze.call_args.args[3], "")
        self.assertEqual(analyze.call_args.kwargs["user_hashtags"], [])
        self.assertEqual(save.call_args.kwargs["original_url"], url)
        self.assertEqual(save.call_args.kwargs["forwarded_text"], caption)
        self.assertEqual(save.call_args.kwargs["media_metadata"]["media_group_id"], "group-1")
        self.assertEqual(save.call_args.kwargs["attachment"][0], b"media")

    async def test_forwarded_link_fetches_original_page_and_keeps_both_texts(self):
        url = "https://example.org/article"
        text = f"分享理由 #原标签\n{url}"
        fetched = SimpleNamespace(source_type="Web", clean_url=url, title="原网页", content="来源网站正文",
                                  status="PASS", note="", channel="mock", fetch_tier="mock")
        with (patch.object(app, "_find_duplicate", AsyncMock(return_value=None)),
              patch.object(app, "smart_fetch", AsyncMock(return_value=fetched)) as fetch,
              patch.object(app, "analyze_content", AsyncMock(return_value=(self.analysis, {}, "test"))) as analyze,
              patch.object(app, "save_to_notion", AsyncMock(return_value=(self.saved_url, False))) as save):
            await app._archive_message(text, [], {"forward_origin": self.author})
        fetch.assert_awaited_once_with(url)
        self.assertEqual(analyze.call_args.kwargs["user_hashtags"], [])
        self.assertEqual(save.call_args.args[4:6], (fetched.content, ""))
        self.assertEqual(save.call_args.kwargs["forwarded_text"], text)

    async def test_explicit_reply_requires_same_sender_and_chat(self):
        target = {"message_id": 6, "chat": {"id": 123}, "from": {"id": 123}}
        baseline = {"message_id": 8, "chat": {"id": 123}, "from": {"id": 123}, "text": "批注：补充说明"}
        with (patch.dict(os.environ, {"ALLOWED_USER_ID": "123"}),
              patch.object(app, "reply", AsyncMock()) as reply,
              patch.object(app, "_archive_message", AsyncMock()) as archive,
              patch.object(app, "append_telegram_note", AsyncMock(return_value=self.saved_url)) as append):
            for reply_target in ({**target, "from": {"id": 555}}, {**target, "chat": {"id": 555}}, target):
                self.assertTrue(await app._process_message({"message": {**baseline, "reply_to_message": reply_target}}))
        append.assert_awaited_once_with(123, 6, 8, "补充说明")
        archive.assert_not_awaited()
        self.assertIn(self.saved_url, reply.call_args.args[1])

    async def test_direct_user_caption_without_prefix_becomes_personal_note(self):
        msg = {"message_id": 21, "chat": {"id": 123}, "from": {"id": 123}, "video": {
            "file_id": "vid-direct", "mime_type": "video/mp4", "file_name": "door.mp4", "duration": 4, "file_size": 1024,
        }}
        media = app.extract_media(msg)
        with (patch.object(app, "_get_tg_client"),
              patch.object(app, "download_media", AsyncMock(return_value=b"mp4")),
              patch.object(app, "analyze_content", AsyncMock(return_value=(self.analysis, {}, "gemini-3.5-flash-lite"))) as analyze,
              patch.object(app, "save_to_notion", AsyncMock(return_value=(self.saved_url, False))) as save):
            receipt, _ = await app._archive_message("这个设计思路不错，记录一下", [], msg, media)
        self.assertEqual(analyze.call_args.args[3], "这个设计思路不错，记录一下")
        self.assertEqual(save.call_args.args[5], "这个设计思路不错，记录一下")
        self.assertEqual(save.call_args.kwargs["accompanying_text"], "")
        self.assertEqual(save.call_args.kwargs["forwarded_text"], "")
        self.assertIn("本人直发", receipt)


class TestNotionTelegramProvenance(unittest.IsolatedAsyncioTestCase):
    async def test_source_sections_and_idempotent_note_append(self):
        blocks = []
        page = {"id": "page-1", "url": "https://notion.so/p/page-1"}
        schema = {"Name": {"type": "title"}, "TelegramKey": {"type": "rich_text"}}
        pages = []
        writes = []

        def handle(request):
            path = request.url.path
            if path == "/v1/databases/archive":
                return httpx.Response(200, json={"data_sources": [{"id": "archive"}]})
            if path == "/v1/data_sources/archive/query":
                self.assertEqual(json.loads(request.content)["filter"],
                                 {"property": "TelegramKey", "rich_text": {"equals": "123:6"}})
                return httpx.Response(200, json={"results": pages})
            if path == "/v1/data_sources/archive":
                return httpx.Response(200, json={"properties": schema})
            if path == "/v1/pages" and request.method == "POST":
                body = json.loads(request.content)
                self.assertEqual(body["properties"]["TelegramKey"]["rich_text"][0]["text"]["content"], "123:6")
                blocks.extend(body["children"])
                pages.append(page)
                return httpx.Response(200, json=page)
            self.assertEqual(path, "/v1/blocks/page-1/children")
            if request.method == "PATCH":
                writes.append(json.loads(request.content))
                blocks.extend(writes[-1]["children"])
                return httpx.Response(503)
            return httpx.Response(200, json={"results": blocks})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with (patch.dict(os.environ, {"NOTION_DB_ARCHIVE": "archive", "NOTION_DB_INBOX": ""}),
                  patch.object(notion_writer, "_ds_cache", {}),
                  patch.object(notion_writer, "_schema_cache", {}),
                  patch.object(notion_writer, "_get_client", return_value=client)):
                await notion_writer.save_to_notion(
                    TestTelegramProvenance.analysis, "Telegram", "", "PASS", "", "",
                    telegram_message={"chat_id": 123, "message_id": 6},
                    forwarded_text="作者配文", forward_metadata={"来源类型": "频道", "链接": "https://t.me/channel/31"},
                    accompanying_text="带来的说明",
                )
                before = json.dumps(blocks, ensure_ascii=False)
                self.assertIn("Telegram 转发原文", before)
                self.assertIn("作者配文", before)
                self.assertIn("随附说明（来源未确认）", before)
                self.assertIn('"type": "callout"', before)
                self.assertIn('"type": "quote"', before)
                self.assertIn("https://t.me/channel/31", before)
                self.assertNotIn("个人批注", before)
                urls = await asyncio.gather(*(notion_writer.append_telegram_note(123, 6, 8, "我的补充")
                                              for _ in range(2)))

        self.assertEqual(urls, [page["url"], page["url"]])
        self.assertEqual(len(writes), 1)
        self.assertIn("我的补充", json.dumps(blocks, ensure_ascii=False))
