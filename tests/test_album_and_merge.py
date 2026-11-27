import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import album_aggregator
import app
import notion_writer
from clip_store import ClipStore
from utils import extract_project_key


class TestAlbumAndCrossSourceMerge(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        self.env_patch = patch.dict(os.environ, {
            "ALLOWED_USER_ID": "123",
            "CLIPPER_DB_PATH": self.db_path,
            "ALBUM_SILENCE_SECONDS": "0.05",
            "ALBUM_MAX_WAIT_SECONDS": "0.5",
            "NOTION_DB_ARCHIVE": "archive",
            "NOTION_DB_INBOX": "",
            "NOTION_TOKEN": "test-notion",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        album_aggregator.coordinator = album_aggregator.AlbumCoordinator()

    async def test_muselink_out_of_order_album_merges_once_without_extra_llm(self):
        # 乱序到达：第2条先到无caption，第1条后到带MuseLink介绍与GitHub链接，第3条带第2张截图
        updates = [
            {"update_id": 102, "message": {"message_id": 2, "chat": {"id": 123}, "from": {"id": 123},
                                           "media_group_id": "mg-muselink", "photo": [{"file_id": "f2", "width": 800, "height": 600}]}},
            {"update_id": 101, "message": {"message_id": 1, "chat": {"id": 123}, "from": {"id": 123},
                                           "media_group_id": "mg-muselink", "photo": [{"file_id": "f1", "width": 800, "height": 600}],
                                           "caption": "MuseLink 开源控制面板介绍与安装指南 https://github.com/owner/muselink"}},
            {"update_id": 103, "message": {"message_id": 3, "chat": {"id": 123}, "from": {"id": 123},
                                           "media_group_id": "mg-muselink", "photo": [{"file_id": "f3", "width": 800, "height": 600}]}},
        ]
        sent = []

        async def fake_tg(method, payload):
            sent.append((method, payload))
            if method == "sendMessage":
                return {"result": {"message_id": 500}}
            return {"ok": True}

        fetched = SimpleNamespace(source_type="GitHub", clean_url="https://github.com/owner/muselink",
                                  title="owner/muselink", content="README 正文", status="PASS", note="",
                                  channel="GitHub API", fetch_tier="Tier1-API", model_content="README 正文", reference_links=())
        analysis = app.AnalysisResult(title="MuseLink", category="技术工具与开源", tags=["开源项目"],
                                      summary="摘要", key_points=["要点"])
        async def fake_download(client, token, media):
            return {"f1": b"img1", "f2": b"img2", "f3": b"img3"}[media.file_id]

        with (patch.object(app, "tg_call", side_effect=fake_tg),
              patch.object(app, "_get_tg_client"),
              patch.object(app, "download_media", side_effect=fake_download),
              patch.object(app, "smart_fetch", AsyncMock(return_value=fetched)) as fetch,
              patch.object(app, "plan_album_units_llm", AsyncMock()) as planner,
              patch.object(app, "analyze_content", AsyncMock(return_value=(analysis, {}, "flash-lite"))) as analyze,
              patch.object(app, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/muselink", False))) as save):
            await asyncio.gather(*(app.process_update(u) for u in updates))

        planner.assert_not_awaited()  # FLYX/MuseLink 单项目介绍多图不额外调用分组模型
        fetch.assert_awaited_once()
        analyze.assert_awaited_once()
        self.assertEqual(len(analyze.call_args.kwargs["media_parts"]), 3)
        save.assert_awaited_once()
        self.assertEqual(len(save.call_args.kwargs["attachments"]), 3)
        # 顺序按 message_id (1, 2, 3) 排序
        self.assertEqual([att[0] for att in save.call_args.kwargs["attachments"]], [b"img1", b"img2", b"img3"])
        edits = [p for m, p in sent if m == "editMessageText"]
        self.assertEqual(len(edits), 1)
        self.assertIn("3 条素材 → 1 篇笔记", edits[0]["text"])

    async def test_mixed_5_photos_split_3_plus_2_and_skip_url_dedup(self):
        updates = [
            {"update_id": 200 + i, "message": {
                "message_id": i, "chat": {"id": 123}, "from": {"id": 123},
                "media_group_id": "mg-home", "photo": [{"file_id": f"f{i}", "width": 400, "height": 400}],
                **({"caption": "家装灵感 https://youtu.be/shared123"} if i == 1 else {}),
            }} for i in range(1, 6)
        ]
        planner_resp = {
            "mode": "mixed", "confidence": 0.9,
            "groups": [
                {"member_ids": [1, 2, 3], "topic": "齐平移门滑轨", "reason": "同一设计对象"},
                {"member_ids": [4, 5], "topic": "洗漱岛台", "reason": "独立设计点"},
            ],
        }
        fetched = SimpleNamespace(source_type="YouTube", clean_url="https://youtu.be/shared123",
                                  title="家装合集", content="视频字幕", status="PASS", note="",
                                  channel="YouTube", fetch_tier="Video-Pipeline")
        analysis = app.AnalysisResult(title="家装设计", category="生活与休闲参考", tags=["家装"],
                                      summary="摘要", key_points=["要点"])
        saved_urls = iter(["https://notion.so/p/rail", "https://notion.so/p/island"])

        with (patch.object(app, "tg_call", AsyncMock(return_value={"result": {"message_id": 600}})),
              patch.object(app, "_get_tg_client"),
              patch.object(app, "download_media", AsyncMock(side_effect=[f"b{i}".encode() for i in range(1, 6)])),
              patch.object(app, "smart_fetch", AsyncMock(return_value=fetched)) as fetch,
              patch.object(app, "plan_album_units_llm", AsyncMock(return_value=planner_resp)),
              patch.object(app, "analyze_content", AsyncMock(return_value=(analysis, {}, "flash-lite"))),
              patch.object(app, "save_to_notion", AsyncMock(side_effect=lambda *a, **k: (next(saved_urls), False))) as save):
            await asyncio.gather(*(app.process_update(u) for u in updates))

        fetch.assert_awaited_once()  # 组内缓存，共享 URL 只抓一次
        self.assertEqual(save.await_count, 2)
        for call in save.call_args_list:
            self.assertFalse(call.kwargs["deduplicate_by_url"])
        self.assertEqual(len(save.call_args_list[0].kwargs["attachments"]), 3)
        self.assertEqual(len(save.call_args_list[1].kwargs["attachments"]), 2)
        self.assertEqual(save.call_args_list[0].kwargs["telegram_message"]["unit_index"], 0)
        self.assertEqual(save.call_args_list[1].kwargs["telegram_message"]["unit_index"], 1)

    async def test_late_album_member_enriches_existing_page(self):
        store = ClipStore(self.db_path)
        akey = album_aggregator.coordinator.album_key(123, 123, "mg-late")
        store.save_album_unit(akey, 0, 123, "123:1", "https://notion.so/p/existing", [1, 2])
        late = {"update_id": 303, "message": {
            "message_id": 3, "chat": {"id": 123}, "from": {"id": 123},
            "media_group_id": "mg-late", "photo": [{"file_id": "f3", "width": 500, "height": 500}],
        }}
        with (patch.object(app, "tg_call", AsyncMock(return_value={"ok": True})),
              patch.object(app, "_get_tg_client"),
              patch.object(app, "download_media", AsyncMock(return_value=b"late-img")),
              patch.object(app, "enrich_notion_page", AsyncMock(return_value=("https://notion.so/p/existing", False))) as enrich):
            await app.process_update(late)
        enrich.assert_awaited_once()
        self.assertEqual(enrich.call_args.kwargs["source_key"], "album-late-3")

    def test_project_key_distinguishes_primary_subject_from_lists(self):
        self.assertEqual(
            extract_project_key("https://x.com/user/status/1", "推荐开源项目 https://github.com/aipoch/open-science", "X"),
            "github:aipoch/open-science",
        )
        self.assertEqual(
            extract_project_key("https://example.com/list", "对比 https://github.com/a/one 和 https://github.com/b/two", "Web"),
            "",
        )

    async def test_cross_source_open_science_x_and_github_merge_into_one_page(self):
        pages: dict[str, dict] = {}
        blocks_by_page: dict[str, list] = {}
        schema = {"Name": {"type": "title"}, "URL": {"type": "url"}, "Tags": {"type": "multi_select"},
                  "TelegramKey": {"type": "rich_text"}, "ProjectKey": {"type": "rich_text"}}

        def handle(request):
            path = request.url.path
            if path == "/v1/databases/archive":
                return httpx.Response(200, json={"data_sources": [{"id": "archive"}]})
            if path == "/v1/data_sources/archive":
                return httpx.Response(200, json={"properties": schema})
            if path == "/v1/data_sources/archive/query":
                filt = json.dumps(json.loads(request.content), ensure_ascii=False)
                for p in pages.values():
                    if p.get("project_key") and p["project_key"] in filt:
                        return httpx.Response(200, json={"results": [p]})
                return httpx.Response(200, json={"results": []})
            if path == "/v1/pages" and request.method == "POST":
                body = json.loads(request.content)
                pid = "page-open-science"
                pk = (body["properties"].get("ProjectKey") or {}).get("rich_text", [{}])[0].get("text", {}).get("content", "")
                page_obj = {"id": pid, "url": f"https://notion.so/p/{pid}", "project_key": pk,
                            "properties": body["properties"]}
                pages[pid] = page_obj
                blocks_by_page[pid] = list(body["children"])
                return httpx.Response(200, json=page_obj)
            if path.startswith("/v1/pages/") and request.method == "GET":
                pid = path.split("/")[-1]
                return httpx.Response(200, json=pages[pid])
            if path.startswith("/v1/pages/") and request.method == "PATCH":
                pid = path.split("/")[-1]
                pages[pid]["properties"]["Tags"] = json.loads(request.content)["properties"]["Tags"]
                return httpx.Response(200, json=pages[pid])
            if path.endswith("/children") and request.method == "GET":
                pid = path.split("/")[3]
                return httpx.Response(200, json={"results": blocks_by_page.get(pid, []), "has_more": False})
            if path.endswith("/children") and request.method == "PATCH":
                pid = path.split("/")[3]
                blocks_by_page[pid].extend(json.loads(request.content)["children"])
                return httpx.Response(200, json={"results": []})
            return httpx.Response(404)

        x_fetched = SimpleNamespace(source_type="X", clean_url="https://x.com/alice/status/99",
                                    title="X 推文", content="介绍 Open-Science: https://github.com/aipoch/open-science",
                                    status="PASS", note="", channel="FxTwitter", fetch_tier="Tier1-API")
        gh_fetched = SimpleNamespace(source_type="GitHub", clean_url="https://github.com/aipoch/open-science",
                                     title="aipoch/open-science", content="Stars: 1200\nREADME 内容",
                                     status="PASS", note="", channel="GitHub API", fetch_tier="Tier1-API")
        res1 = app.AnalysisResult(title="Open-Science (X)", category="技术工具与开源", tags=["科研"],
                                  summary="推文简介", key_points=["亮点"])
        res2 = app.AnalysisResult(title="aipoch/open-science", category="技术工具与开源", tags=["开源项目"],
                                  summary="仓库详情", key_points=["MIT"])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with (patch.object(notion_writer, "_ds_cache", {}),
                  patch.object(notion_writer, "_schema_cache", {}),
                  patch.object(notion_writer, "_get_client", return_value=client),
                  patch.object(app, "smart_fetch", AsyncMock(side_effect=[x_fetched, gh_fetched])),
                  patch.object(app, "analyze_content", AsyncMock(side_effect=[(res1, {}, "m"), (res2, {}, "m")]))):
                r1, _ = await app._archive_message("https://x.com/alice/status/99", [], {"chat": {"id": 123}, "message_id": 10})
                r2, _ = await app._archive_message("https://github.com/aipoch/open-science", [], {"chat": {"id": 123}, "message_id": 11})

        self.assertIn("已归档", r1)
        self.assertIn("增量合并", r2)
        self.assertEqual(len(pages), 1)
        merged_tags = [t["name"] for t in pages["page-open-science"]["properties"]["Tags"]["multi_select"]]
        self.assertIn("科研", merged_tags)
        self.assertIn("开源项目", merged_tags)

    def test_control_hashtags_are_stripped_from_unit_tags(self):
        m1 = album_aggregator.parse_album_member({
            "message_id": 1, "caption": "笔记说明 #合并 #AI", "photo": [{"file_id": "a"}],
        })
        m2 = album_aggregator.parse_album_member({
            "message_id": 2, "photo": [{"file_id": "b"}],
        })
        units = asyncio.run(album_aggregator.plan_knowledge_units([m1, m2], {}))
        self.assertEqual(len(units), 1)
        mats = album_aggregator.combine_unit_materials(units[0])
        self.assertEqual(mats["user_tags"], ["AI"])
        self.assertNotIn("合并", mats["user_tags"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
