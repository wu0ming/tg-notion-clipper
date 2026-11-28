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

import category_config
import gemini_processor as gemini
import router
import telegram_media
import video_pipeline
from utils import build_entity_sections


class TestVideoAndEntities(unittest.IsolatedAsyncioTestCase):
    def test_telegram_media_extracts_video_and_video_note(self):
        msg_video = {"message_id": 10, "video": {"file_id": "v1", "file_unique_id": "u1", "mime_type": "video/mp4",
                                                 "file_name": "demo.mp4", "file_size": 1024, "duration": 45, "width": 720, "height": 1280}}
        m1 = telegram_media.extract_media(msg_video)
        self.assertIsNotNone(m1)
        self.assertEqual((m1.input_type, m1.mime_type, m1.duration, m1.width), ("Video", "video/mp4", 45, 720))

        msg_doc = {"message_id": 11, "document": {"file_id": "v2", "file_name": "screen.mov", "mime_type": "application/octet-stream"}}
        m2 = telegram_media.extract_media(msg_doc)
        self.assertEqual((m2.input_type, m2.mime_type), ("Video", "video/quicktime"))

        msg_note = {"message_id": 12, "video_note": {"file_id": "v3", "duration": 12}}
        m3 = telegram_media.extract_media(msg_note)
        self.assertEqual((m3.input_type, m3.duration), ("Video", 12))

    def test_video_intent_and_timestamp_links(self):
        self.assertEqual(video_pipeline.classify_video_intent("把视频中的动作拆解出来，带上讲解和截图"), "visual_steps")
        self.assertEqual(video_pipeline.classify_video_intent("简单总结一下"), "standard_summary")
        self.assertEqual(video_pipeline.timestamp_link("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 95),
                         "https://youtu.be/dQw4w9WgXcQ?t=95")
        self.assertEqual(video_pipeline.timestamp_link("https://www.bilibili.com/video/BV1xx411c7mD?p=2", 42),
                         "https://www.bilibili.com/video/BV1xx411c7mD?p=2&t=42")

    async def test_bilibili_tier1_metadata_and_subtitles_zero_video_download(self):
        def handle(request):
            if request.url.path == "/x/web-interface/view":
                return httpx.Response(200, json={"code": 0, "data": {
                    "title": "肩袖康复训练教程", "owner": {"name": "物理治疗师"}, "duration": 360,
                    "desc": "动作要点说明", "cid": 999, "pages": [{"cid": 999, "part": "P1", "duration": 360}],
                }})
            if request.url.path == "/x/player/v2":
                return httpx.Response(200, json={"code": 0, "data": {
                    "subtitle": {"subtitles": [{"subtitle_url": "//aisubtitle.hdslb.com/bfs/sub.json"}]}
                }})
            if request.url.host == "aisubtitle.hdslb.com":
                return httpx.Response(200, json={"body": [
                    {"from": 10, "to": 25, "content": "第一步弹力带外旋"},
                    {"from": 60, "to": 80, "content": "注意肩胛骨保持稳定"},
                ]})
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            asset = await video_pipeline.fetch_video_asset(client, "https://www.bilibili.com/video/BV1xx411c7mD", use_cache=False)

        self.assertEqual((asset.source_type, asset.access_status, asset.duration_sec), ("Bilibili", "PASS", 360))
        self.assertIn("[00:10] 第一步弹力带外旋", asset.format_transcript())

    async def test_instruction_video_degrades_honestly_without_ffmpeg(self):
        asset = video_pipeline.VideoAsset(
            source_type="YouTube", canonical_id="youtube:abc123", url="https://youtu.be/abc123",
            title="康复动作教学", duration_sec=180,
            transcript_segments=[{"start_sec": 15, "end_sec": 30, "text": "动作一：慢速下蹲"}],
            access_status="PASS",
        )
        cats = [{"name": "生活与休闲参考"}]
        vision_json = {
            "title": "康复动作教学拆解", "category": "生活与休闲参考", "tags": ["康复训练"],
            "summary": "包含下蹲动作。", "key_points": ["保持核心收紧"],
            "steps": [{"title": "慢速下蹲", "start_sec": 15, "end_sec": 30, "explanation": "原视频强调慢速控制", "cues": ["膝盖对齐脚尖"]}],
        }
        with (patch.object(video_pipeline, "fetch_video_asset", AsyncMock(return_value=asset)),
              patch.object(gemini, "plan_video_keyframes", AsyncMock(return_value={"steps": [{"target_sec": 18}]})),
              patch.object(video_pipeline, "extract_link_keyframes", AsyncMock(return_value=([], "运行环境未安装 yt-dlp 或 ffmpeg"))),
              patch.object(gemini, "analyze_video_steps", AsyncMock(return_value=vision_json))):
            res = await video_pipeline.analyze_instruction_video(None, "https://youtu.be/abc123", "拆解动作并截图", cats)

        self.assertEqual(res["status"], "PARTIAL")
        self.assertIn("未安装 yt-dlp 或 ffmpeg", res["note"])
        self.assertEqual(res["steps"][0]["timestamp_url"], "https://youtu.be/abc123?t=15")

    async def test_l0_routing_flex_fallback_and_public_private_split(self):
        plan_gh = gemini.route_l0(url="https://github.com/owner/repo", source_type="GitHub")
        self.assertEqual((plan_gh.entity_type, plan_gh.candidate_category, plan_gh.route_tier),
                         ("技术开源", "技术工具与开源", "L0"))

        # Flex 触发 503/Timeout 时单次受控回退 standard；且纯公开链接才能用 Free Key
        text = json.dumps({"title": "测试", "category": "技术", "tags": [], "summary": "摘要", "key_points": []})
        ok_resp = SimpleNamespace(text=text, usage_metadata=None)
        calls = []

        async def fake_generate_content(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise TimeoutError("flex timeout")
            return ok_resp

        manager = AsyncMock()
        manager.__aenter__.return_value = SimpleNamespace(models=SimpleNamespace(generate_content=fake_generate_content))
        used_free = []

        def fake_client(*, allow_free=False):
            used_free.append(allow_free)
            return manager

        with (patch.dict(os.environ, {"GEMINI_MODEL": "", "GEMINI_FALLBACK_MODEL": "", "GEMINI_COMPLEX_MODEL": "",
                                      "GEMINI_SERVICE_TIER": "flex", "GEMINI_API_KEY_FREE": "free-key"}),
              patch.object(gemini, "_client", side_effect=fake_client),
              patch.object(gemini, "get_categories", AsyncMock(return_value=([{"name": "技术"}], "test")))):
            await gemini.analyze_content("Web", "标题", "正文", "", "PASS", url="https://example.com/p", is_public_only=True)
            await gemini.analyze_content("Web", "标题", "正文", "私人批注", "PASS", url="https://example.com/p", is_public_only=True)

        self.assertEqual(used_free, [True, False])  # 有私人批注时绝不走 Free Tier
        self.assertEqual(str(calls[0]["config"].service_tier.value), "flex")
        self.assertEqual(str(calls[1]["config"].service_tier.value), "standard")

    def test_rules_yaml_hot_reload_and_entity_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "rules.yaml"
            p.write_text("categories:\n  - name: A\n    description: d1\n", encoding="utf-8")
            c1 = category_config.load_yaml_rules(p)
            p.write_text("categories:\n  - name: B\n    description: d2\n", encoding="utf-8")
            c2 = category_config.load_yaml_rules(p)
            self.assertEqual((c1[0]["name"], c2[0]["name"]), ("A", "B"))

        blocks = build_entity_sections("行业研报/政策法规", {"发布机构与时间": "欧盟委员会 2026", "核心义务与指标": "电池护照"})
        rendered = json.dumps(blocks, ensure_ascii=False)
        self.assertIn("结构化模板 · 行业研报/政策法规", rendered)
        self.assertIn("【发布机构与时间】欧盟委员会 2026", rendered)
        self.assertIn("【业务影响】未提及", rendered)  # 缺失字段显式标注未提及，不编造

    def test_jina_local_token_budget_enforcement(self):
        long_text = "开头内容" + ("中间正文" * 2000) + "结尾结论"
        trimmed = router._apply_jina_token_budget(long_text, "500")
        self.assertLessEqual(len(trimmed), 2050)
        self.assertTrue(trimmed.startswith("开头内容"))
        self.assertTrue(trimmed.endswith("结尾结论"))

    async def test_telegram_video_file_archive_and_oversize_fallback(self):
        import app
        msg = {"message_id": 88, "chat": {"id": 123}, "video": {
            "file_id": "vid-1", "mime_type": "video/mp4", "file_name": "decor.mp4", "duration": 25, "file_size": 1024,
        }}
        media = telegram_media.extract_media(msg)
        analysis = app.AnalysisResult(title="家居设计视频", category="生活与休闲参考", tags=["示例"],
                                      summary="原木风玄关柜设计。", key_points=["隐藏式踢脚线"],
                                      extracted_text="[00:05] 展示隐形门设计", entity_type="视频教程/访谈")
        with (patch.object(app, "_get_tg_client"),
              patch.object(app, "download_media", AsyncMock(return_value=b"mp4-bytes")),
              patch.object(app, "analyze_content", AsyncMock(return_value=(analysis, {}, "gemini-3.8-flash"))) as analyze,
              patch.object(app, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/vid", False))) as save):
            receipt, reaction = await app._archive_message("看这个玄关设计", [], msg, media)
        self.assertEqual(reaction, "👌")
        self.assertEqual(analyze.call_args.kwargs["input_type"], "Video")
        self.assertEqual(save.call_args.kwargs["input_type"], "Video")
        self.assertIn("家居设计视频", receipt)

        # 超出 20 MB 下载上限时保留元数据明确降级
        big_msg = {"message_id": 89, "chat": {"id": 123}, "video": {
            "file_id": "vid-big", "mime_type": "video/mp4", "duration": 180, "file_size": 30 * 1024 * 1024,
        }}
        big_media = telegram_media.extract_media(big_msg)
        with (patch.object(app, "_get_tg_client"),
              patch.object(app, "get_categories", AsyncMock(return_value=([{"name": "待整理"}], "mock"))),
              patch.object(app, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/big", False))) as save_big):
            receipt2, reaction2 = await app._archive_message("大视频", [], big_msg, big_media)
        self.assertEqual(reaction2, "🤔")
        self.assertEqual(save_big.call_args.args[3], "FALLBACK")
        self.assertIn("20 MB", receipt2)

    async def test_short_video_uses_flash_lite_and_low_media_resolution(self):
        plan_short = gemini.route_l0(source_type="Telegram", input_type="Video", media_bytes_len=500_000)
        self.assertEqual(plan_short.target_model, gemini.DEFAULT_MODEL)
        self.assertEqual(plan_short.thinking_level, "MINIMAL")

        reply = json.dumps({"title": "4秒产品演示", "category": "技术", "tags": ["示例"], "summary": "摘要",
                            "key_points": ["要点"], "extracted_text": "[00:01] 演示画面"})
        manager = AsyncMock()
        gen = AsyncMock(return_value=SimpleNamespace(text=reply, usage_metadata=None))
        manager.__aenter__.return_value = SimpleNamespace(models=SimpleNamespace(generate_content=gen))
        with (patch.dict(os.environ, {"GEMINI_MODEL": "", "GEMINI_FALLBACK_MODEL": "", "GEMINI_COMPLEX_MODEL": ""}),
              patch.object(gemini, "_client", return_value=manager),
              patch.object(gemini, "get_categories", AsyncMock(return_value=([{"name": "技术"}], "test")))):
            await gemini.analyze_content("Telegram", "clip.mp4", "", "产品演示", "PASS",
                                         media_bytes=b"mp4", media_mime="video/mp4", input_type="Video")
        self.assertEqual(gen.await_args.kwargs["model"], gemini.DEFAULT_MODEL)
        self.assertEqual(gen.await_args.kwargs["config"].media_resolution, gemini.types.MediaResolution.MEDIA_RESOLUTION_LOW)

    async def test_long_direct_video_triggers_keyframe_cost_saving(self):
        import app
        msg = {"message_id": 90, "chat": {"id": 123}, "video": {
            "file_id": "vid-long", "mime_type": "video/mp4", "file_name": "long.mp4", "duration": 120, "file_size": 5_000_000,
        }}
        media = telegram_media.extract_media(msg)
        analysis = app.AnalysisResult(title="长视频抽帧", category="技术", tags=["视频"], summary="摘要", key_points=["要点"])
        with (patch.object(app, "_get_tg_client"),
              patch.object(app, "download_media", AsyncMock(return_value=b"mp4-long")),
              patch.object(app, "extract_frames_from_bytes", return_value=[(10, b"jpg1", "image/jpeg"), (60, b"jpg2", "image/jpeg")]) as extract_frames,
              patch.object(app, "analyze_content", AsyncMock(return_value=(analysis, {}, "gemini-3.5-flash-lite"))) as analyze,
              patch.object(app, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/long", False))) as save):
            await app._archive_message("看看这个", [], msg, media)
        extract_frames.assert_called_once()
        self.assertIsNone(analyze.call_args.kwargs["media_bytes"])
        self.assertEqual(len(analyze.call_args.kwargs["media_parts"]), 2)
        self.assertEqual(save.call_args.kwargs["fetch_tier"], "Video-Keyframes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
