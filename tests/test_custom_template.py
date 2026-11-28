import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
import notion_writer
import template_config


def _rt(text: str, *, bold: bool = False, color: str = "default") -> list[dict]:
    item: dict = {"type": "text", "plain_text": text, "text": {"content": text}}
    if bold or color != "default":
        item["annotations"] = {"bold": bold, "color": color}
    return [item]


class TestCustomTemplate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        template_config.clear_template_cache()

    def tearDown(self):
        template_config.clear_template_cache()

    async def test_auto_discovers_template_page_caches_5min_and_renders_into_notion_page(self):
        tpl_top_blocks = [
            {
                "id": "b-help", "type": "heading_2", "has_children": True,
                "heading_2": {"rich_text": _rt("使用说明（不会写入剪藏页）"), "is_toggleable": True},
            },
            {"id": "b-div", "type": "divider", "has_children": False, "divider": {}},
            {
                "id": "b-note", "type": "callout", "has_children": False,
                "callout": {"rich_text": _rt("💡 我的批注：{{user_note}}"), "icon": {"type": "emoji", "emoji": "💡"}, "color": "blue_background"},
            },
            {
                "id": "b-sum", "type": "callout", "has_children": False,
                "callout": {"rich_text": [{"type": "text", "plain_text": "摘要：", "text": {"content": "摘要："}, "annotations": {"bold": True}},
                                          {"type": "text", "plain_text": "{{summary}}", "text": {"content": "{{summary}}"}}],
                            "icon": {"type": "emoji", "emoji": "🤖"}},
            },
            {"id": "b-url", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": _rt("{{url}}")}},
            {"id": "b-hkp", "type": "heading_2", "has_children": False, "heading_2": {"rich_text": _rt("✨ 精华要点")}},
            {"id": "b-kp", "type": "bulleted_list_item", "has_children": False, "bulleted_list_item": {"rich_text": _rt("{{key_points}}")}},
            {
                "id": "b-ocr", "type": "heading_2", "has_children": True,
                "heading_2": {"rich_text": _rt("提取文字"), "is_toggleable": True},
            },
            {
                "id": "b-raw", "type": "heading_2", "has_children": True,
                "heading_2": {"rich_text": _rt("📄 原文存档"), "is_toggleable": True},
            },
            {"id": "b-todo", "type": "to_do", "has_children": False, "to_do": {"rich_text": _rt("已复盘"), "checked": False}},
        ]
        tpl_children = {
            "b-help": [{"id": "b-help-1", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": _rt("说明正文")}}],
            "b-ocr": [{"id": "b-ocr-1", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": _rt("{{extracted_text}}")}}],
            "b-raw": [{"id": "b-raw-1", "type": "paragraph", "has_children": False, "paragraph": {"rich_text": _rt("{{raw_content}}")}}],
        }
        created_bodies = []
        tpl_reads = 0

        def handle(request):
            nonlocal tpl_reads
            path = request.url.path
            if path == "/v1/databases/archive":
                return httpx.Response(200, json={
                    "parent": {"type": "page_id", "page_id": "parent-info-archive"},
                    "data_sources": [{"id": "archive"}],
                })
            if path == "/v1/data_sources/archive":
                return httpx.Response(200, json={"properties": {"Name": {"type": "title"}, "URL": {"type": "url"}}})
            if path == "/v1/data_sources/archive/query":
                return httpx.Response(200, json={"results": []})
            if path == "/v1/blocks/parent-info-archive/children":
                return httpx.Response(200, json={"results": [
                    {"id": "other-db", "type": "child_database", "child_database": {"title": "归档总表"}},
                    {"id": "tpl-page-1", "type": "child_page", "child_page": {"title": "检查模板"}},
                ], "has_more": False})
            if path == "/v1/blocks/tpl-page-1/children":
                tpl_reads += 1
                return httpx.Response(200, json={"results": tpl_top_blocks, "has_more": False})
            if path.startswith("/v1/blocks/") and path.endswith("/children"):
                bid = path.split("/")[3]
                return httpx.Response(200, json={"results": tpl_children.get(bid, []), "has_more": False})
            if path == "/v1/pages" and request.method == "POST":
                body = json.loads(request.content)
                created_bodies.append(body)
                return httpx.Response(200, json={"id": "created-1", "url": "https://notion.so/p/created-1"})
            return httpx.Response(404)

        res1 = app.AnalysisResult(title="标题", category="技术工具与开源", tags=["AI"], summary="这是AI摘要", key_points=["要点A", "要点B"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with (patch.dict(os.environ, {"NOTION_DB_ARCHIVE": "archive", "NOTION_DB_INBOX": "", "NOTION_PAGE_TEMPLATE": ""}),
                  patch.object(notion_writer, "_ds_cache", {}),
                  patch.object(notion_writer, "_schema_cache", {}),
                  patch.object(notion_writer, "_get_client", return_value=client)):
                await notion_writer.save_to_notion(
                    res1, "Web", "https://example.com/1", "PASS", "这是原文正文",
                    user_note="第一条批注", deduplicate_by_url=False,
                )
                self.assertEqual(tpl_reads, 1)
                # 第二次在 5 分钟缓存内直接复用，不重复读取模板页面
                await notion_writer.save_to_notion(
                    res1, "Web", "https://example.com/2", "PASS", "",
                    user_note="", deduplicate_by_url=False,
                )
                self.assertEqual(tpl_reads, 1)

        first = created_bodies[0]["children"]
        first_json = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("使用说明", first_json)  # 说明区不写入剪藏页
        self.assertNotIn("提取文字", first_json)  # 空 extracted_text 的 Toggle 自动隐藏
        self.assertEqual(first[0]["type"], "divider")
        self.assertEqual(first[1]["callout"]["color"], "blue_background")
        self.assertIn("💡 我的批注：第一条批注", json.dumps(first[1], ensure_ascii=False))
        self.assertTrue(first[2]["callout"]["rich_text"][0]["annotations"]["bold"])  # 保留模板加粗样式
        self.assertEqual(first[3], {"object": "block", "type": "bookmark", "bookmark": {"url": "https://example.com/1"}})
        self.assertEqual(first[4]["heading_2"]["rich_text"][0]["text"]["content"], "✨ 精华要点")
        self.assertEqual(first[5]["bulleted_list_item"]["rich_text"][0]["text"]["content"], "要点A")
        self.assertEqual(first[6]["bulleted_list_item"]["rich_text"][0]["text"]["content"], "要点B")
        self.assertEqual(first[7]["heading_2"]["rich_text"][0]["text"]["content"], "📄 原文存档")
        self.assertEqual(first[8]["type"], "to_do")

        # 第二次无批注且无原文正文：对应动态块（批注 callout、原文 Toggle）自动省略，但静态 to_do 保留
        second_json = json.dumps(created_bodies[1]["children"], ensure_ascii=False)
        self.assertNotIn("我的批注", second_json)
        self.assertNotIn("原文存档", second_json)
        self.assertIn("已复盘", second_json)

    def test_empty_flat_heading_pruned_and_safety_nets_preserve_forward_and_media(self):
        tpl = [
            {"type": "heading_2", "heading_2": {"rich_text": _rt("核心要点")}},
            {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt("{{key_points}}")}},
            {"type": "paragraph", "paragraph": {"rich_text": _rt("固定提示语")}},
        ]
        blocks, used = template_config.render_template_blocks(tpl, {"user_note": ""}, {"key_points": []})
        # 核心要点紧跟的 {{key_points}} 为空，但后面还有静态块？注意在下一个边界前没有内容才省略；此处因为同段后有固定提示语，仅省略空的 {{key_points}}。
        self.assertNotIn("key_points", json.dumps(blocks, ensure_ascii=False))
        self.assertIn("固定提示语", json.dumps(blocks, ensure_ascii=False))
        self.assertIn("key_points", used)

        # 若平级 Heading 下只有空的动态占位符，则 Heading 一并隐藏
        tpl2 = [
            {"type": "heading_2", "heading_2": {"rich_text": _rt("核心要点")}},
            {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": _rt("{{key_points}}")}},
            {"type": "divider", "divider": {}},
        ]
        blocks2, _ = template_config.render_template_blocks(tpl2, {}, {"key_points": []})
        self.assertEqual(blocks2, [{"object": "block", "type": "divider", "divider": {}}])

    async def test_telegram_template_and_reload_commands(self):
        with (patch.dict(os.environ, {"ALLOWED_USER_ID": "123"}),
              patch.object(app, "get_categories", AsyncMock(return_value=([{"name": "技术", "description": "desc"}], "Notion 分类配置表"))),
              patch.object(app, "get_template", AsyncMock(return_value=([{"type": "divider"}], "Notion 剪藏模板（1 个顶层区块）", "tpl123"))),
              patch.object(app, "reply", AsyncMock()) as reply_mock):
            await app._process_message({"message": {"message_id": 1, "chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "/template"}})
            await app._process_message({"message": {"message_id": 2, "chat": {"id": 123, "type": "private"}, "from": {"id": 123}, "text": "/reload"}})
        self.assertIn("https://www.notion.so/tpl123", reply_mock.call_args_list[0].args[1])
        self.assertIn("剪藏模板：Notion 剪藏模板", reply_mock.call_args_list[1].args[1])

    def test_dockerfile_copies_template_config(self):
        dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("template_config.py", dockerfile)


if __name__ == "__main__":
    unittest.main(verbosity=2)
