"""
离线自测（无需网络、无需真实密钥）：python tests/test_offline.py
覆盖：URL 提取/归一化/来源识别、微信图片代理、Jina 解析、切块、Notion 载荷、rules.yaml 与输出 Schema、
      Webhook 鉴权、白名单静默忽略、update_id 去重
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.update({"TELEGRAM_BOT_TOKEN": "0:test", "WEBHOOK_SECRET": "test-secret", "ALLOWED_USER_ID": "111,222",
                   "GEMINI_API_KEY": "x", "NOTION_TOKEN": "x", "NOTION_DB_CONFIG": "", "NOTION_DB_ARCHIVE": ""})

import utils  # noqa: E402


class TestUtils(unittest.TestCase):
    def test_extract_url_plain(self):
        url, note = utils.extract_url("这篇讲 PPWR 很清楚 https://example.com/a?utm_source=tg。值得看")
        self.assertEqual(url, "https://example.com/a?utm_source=tg")
        self.assertIn("PPWR", note)
        self.assertNotIn("http", note)

    def test_extract_url_entity_utf16(self):
        text = "😀中文 https://x.com/a/status/1 备注"
        offset = len("😀中文 ".encode("utf-16-le")) // 2
        url, note = utils.extract_url(text, [{"type": "url", "offset": offset, "length": len("https://x.com/a/status/1")}])
        self.assertEqual(url, "https://x.com/a/status/1")
        self.assertEqual(note, "😀中文 备注")

    def test_extract_text_link(self):
        url, note = utils.extract_url("看这里", [{"type": "text_link", "offset": 0, "length": 3, "url": "https://a.com"}])
        self.assertEqual((url, note), ("https://a.com", "看这里"))

    def test_no_url(self):
        self.assertEqual(utils.extract_url("纯文本随手记")[0], None)

    def test_normalize(self):
        n = utils.normalize_url
        self.assertEqual(n("https://twitter.com/jack/status/20/photo/1?s=20&t=abc"), "https://x.com/jack/status/20")
        self.assertEqual(n("https://old.reddit.com/r/Python/comments/abc/title/?utm_source=share&utm_medium=web"),
                         "https://www.reddit.com/r/Python/comments/abc/title")
        self.assertEqual(n("https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=xx&chksm=yy&scene=21#wechat_redirect"),
                         "https://mp.weixin.qq.com/s?__biz=MzA&mid=1&idx=1&sn=xx")
        self.assertEqual(n("https://mp.weixin.qq.com/s/AbCdEf"), "https://mp.weixin.qq.com/s/AbCdEf")
        self.assertEqual(n("v2ex.com/t/123"), "https://www.v2ex.com/t/123")
        self.assertEqual(n("https://example.com/p?id=3&utm_campaign=x&fbclid=1"), "https://example.com/p?id=3")

    def test_detect_source(self):
        d = utils.detect_source
        self.assertEqual(d("https://x.com/a/status/1"), "X")
        self.assertEqual(d("https://www.reddit.com/r/a"), "Reddit")
        self.assertEqual(d("https://www.v2ex.com/t/1"), "V2EX")
        self.assertEqual(d("https://mp.weixin.qq.com/s/a"), "WeChat")
        self.assertEqual(d("https://github.com/a/b"), "GitHub")

    def test_wechat_images(self):
        out = utils.rewrite_wechat_images("![](https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png)")
        self.assertIn("https://wsrv.nl/?url=https%3A%2F%2Fmmbiz.qpic.cn", out)
        self.assertEqual(utils.rewrite_wechat_images(out), out)  # 幂等

    def test_jina_parse(self):
        title, body = utils.parse_jina_output("Title: Hello\n\nURL Source: https://a\n\nMarkdown Content:\n# H\ntext")
        self.assertEqual((title, body), ("Hello", "# H\ntext"))
        title, _ = utils.parse_jina_output("Title: \nURL Source: https://www.reddit.com/x\n\nMarkdown Content:\nbody")
        self.assertEqual(title, "")  # 空标题不能吞掉下一行

    def test_chunk(self):
        text = ("a" * 1000 + "\n") * 5 + "b" * 4000
        chunks = utils.chunk_text(text, 1800)
        self.assertTrue(all(len(c) <= 1800 for c in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_tags(self):
        self.assertEqual(utils.clean_tags(["#ESG", "ESG", "A,B", "", "x" * 80, "多余"], 4), ["ESG", "A/B", "x" * 50, "多余"])

    def test_notion_payload(self):
        props = utils.build_properties("t" * 200, "技术工具与开源", ["AI Agent"], "Web", "https://a.com", "s" * 3000,
                                       "PASS", "2026-11-24T00:00:00+00:00")
        self.assertLessEqual(len(props["Name"]["title"][0]["text"]["content"]), 100)
        self.assertLessEqual(len(props["Summary"]["rich_text"][0]["text"]["content"]), 1900)
        blocks = utils.build_children(["要点"] * 6, "正文" * 30000, "批注", "https://a.com", "PARTIAL", "含视频")
        self.assertLessEqual(len(blocks), 100)
        toggle = blocks[-1]["heading_2"]
        self.assertTrue(toggle["is_toggleable"])
        self.assertLessEqual(len(toggle["children"]), 20)
        for b in toggle["children"]:
            self.assertLessEqual(len(b["paragraph"]["rich_text"][0]["text"]["content"]), 2000)


class TestCategories(unittest.TestCase):
    def test_yaml_fallback_and_schema(self):
        import category_config as cc
        from gemini_processor import response_model, match_category, build_prompt
        cats, src = asyncio.run(cc.get_categories(force=True))
        self.assertEqual(src, "rules.yaml")
        names = tuple(c["name"] for c in cats)
        self.assertGreaterEqual(len(names), 4)
        for n in names:
            self.assertNotIn(",", n)
        model = response_model(names)
        parsed = model.model_validate_json(
            '{"title":"t","category":"%s","tags":["a"],"summary":"s","key_points":["k"]}' % names[1])
        self.assertEqual(parsed.category.value, names[1])
        with self.assertRaises(Exception):
            model.model_validate_json('{"title":"t","category":"不存在","tags":[],"summary":"s","key_points":[]}')
        self.assertEqual(match_category(cats, "乱写")["name"], names[0])
        self.assertIn("原文未抓取成功", build_prompt(cats, "Web", "", "", "", "FALLBACK"))
        self.assertIn("提取重点", build_prompt(cats, "Web", "", "", "", "PASS"))

    def test_parse_config_rows(self):
        from category_config import parse_config_rows

        def row(name, enabled=True, order=None, desc="d"):
            return {"properties": {
                "分类名": {"type": "title", "title": [{"plain_text": name}]},
                "描述": {"type": "rich_text", "rich_text": [{"plain_text": desc}]},
                "启用": {"type": "checkbox", "checkbox": enabled},
                "排序": {"type": "number", "number": order}}}
        rows = parse_config_rows([row("B", order=2), row("A", order=1), row("停用", enabled=False),
                                  row("A", order=3), row(" ", order=0), row("含,逗号", order=5)])
        self.assertEqual([r["name"] for r in rows], ["A", "B", "含、逗号"])
        self.assertEqual(rows[0]["description"], "d")
        self.assertEqual(rows[0]["tagging_rule"], "")


class TestParsers(unittest.TestCase):
    def test_reddit_listing(self):
        from router import parse_reddit_listing
        data = [{"data": {"children": [{"data": {"title": "T", "subreddit": "Python", "author": "a",
                                                  "selftext": "body", "score": 5, "num_comments": 2,
                                                  "created_utc": 1700000000}}]}},
                {"data": {"children": [{"kind": "t1", "data": {"score": 1, "author": "x", "body": "low"}},
                                       {"kind": "t1", "data": {"score": 9, "author": "y", "body": "high"}},
                                       {"kind": "more", "data": {}}]}}]
        title, content = parse_reddit_listing(data)
        self.assertEqual(title, "T")
        self.assertLess(content.index("high"), content.index("low"))  # 按赞数排序
        self.assertIn("2023-11-15", content)

    def test_wechat_direct_parsing(self):
        from router import wechat_direct
        import httpx
        article = ('<html><head><meta property="og:title" content="OG标题"></head><body>'
                   '<h1 id="activity-name"> 电池护照解读 </h1><a id="js_name">某某ESG</a>'
                   '<div id="js_content"><p>' + "正文内容" * 30 + '</p>'
                   '<img data-src="https://mmbiz.qpic.cn/mmbiz_png/abc/640?wx_fmt=png"></div>'
                   '<script>var ct = "1700000000";</script></body></html>')
        deleted = '<html><body><div class="weui-msg">该内容已被发布者删除</div></body></html>'
        picture = ('<html><head><meta property="og:title" content="图片消息"><meta property="og:description" '
                   'content="描述文字"></head><body><script>var picture_page_info_list = '
                   '[{cdn_url: "https://mmbiz.qpic.cn/sz_mmbiz_jpg/xyz/0?wx_fmt=jpeg"}];</script></body></html>')
        pages = {"/a": article, "/d": deleted, "/p": picture}
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=pages[req.url.path]))

        async def run(path):
            async with httpx.AsyncClient(transport=transport) as client:
                return await wechat_direct(client, f"https://mp.weixin.qq.com{path}")
        r = asyncio.run(run("/a"))
        self.assertEqual((r.status, r.title), ("PASS", "电池护照解读"))
        self.assertIn("公众号：某某ESG", r.content)
        self.assertIn("发布时间：2023-11-15", r.content)
        self.assertIn("wsrv.nl/?url=https%3A%2F%2Fmmbiz.qpic.cn", r.content)
        r = asyncio.run(run("/d"))
        self.assertEqual(r.status, "FALLBACK")
        self.assertIn("已被发布者删除", r.note)
        r = asyncio.run(run("/p"))
        self.assertEqual((r.status, r.title), ("PARTIAL", "图片消息"))
        self.assertIn("wsrv.nl", r.content)

    def test_wechat_code_block(self):
        from router import wechat_direct
        import httpx
        page = ('<html><body><h1 id="activity-name">代码</h1><div id="js_content"><p>' + "说明" * 40 + '</p>'
                '<section class="code-snippet__fix"><ul class="code-snippet__line-index"><li></li><li></li></ul>'
                '<pre class="code-snippet__js" data-lang="json"><code><span>{"a": 1,</span></code>'
                '<code><span>"b": 2}</span></code></pre></section></div></body></html>')
        transport = httpx.MockTransport(lambda req: httpx.Response(200, text=page))

        async def run():
            async with httpx.AsyncClient(transport=transport) as client:
                return await wechat_direct(client, "https://mp.weixin.qq.com/s/x")
        r = asyncio.run(run())
        self.assertIn('```json\n{"a": 1,\n"b": 2}\n```', r.content)
        self.assertNotIn("ounter(line", r.content)


class TestWebhook(unittest.TestCase):
    def setUp(self):
        import importlib
        from fastapi.testclient import TestClient
        import app as app_module
        app_module = importlib.reload(app_module)  # 各用例会替换模块里的函数，每次重新加载保证互不影响
        self.app_module = app_module
        self.client = TestClient(app_module.app)

    def test_secret_required(self):
        r = self.client.post("/webhook", json={"update_id": 1})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/webhook", json={"update_id": 1}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        self.assertEqual(r.status_code, 403)

    def test_non_whitelisted_ignored(self):
        calls = []

        async def fake_archive(*a, **k):
            calls.append(a)
            return "x"
        self.app_module._archive_message = fake_archive
        update = {"update_id": 99, "message": {"message_id": 1, "chat": {"id": 5}, "from": {"id": 999}, "text": "hi"}}
        r = self.client.post("/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, [])

    def test_dedupe_and_whitelist(self):
        calls, sent = [], []

        async def fake_archive(text, entities, message, media):
            calls.append(text)
            return "ok", "👌"

        async def fake_tg(method, payload):
            sent.append((method, payload))
            if method == "sendMessage":
                return {"result": {"message_id": 72}}
            return {"ok": True}
        self.app_module._archive_message = fake_archive
        self.app_module.tg_call = fake_tg
        update = {"update_id": 100, "message": {"message_id": 2, "chat": {"id": 5}, "from": {"id": 222}, "text": "随手记"}}
        asyncio.run(self.app_module.process_update(update))
        asyncio.run(self.app_module.process_update(update))  # Telegram 重试
        self.assertEqual(calls, ["随手记"])
        self.assertEqual([method for method, _ in sent],
                         ["setMessageReaction", "sendMessage", "sendChatAction", "setMessageReaction", "editMessageText"])
        self.assertEqual(sent[0][1]["reaction"][0]["emoji"], "👀")
        self.assertEqual(sent[1][1]["reply_parameters"]["message_id"], 2)
        self.assertEqual(sent[3][1]["reaction"][0]["emoji"], "👌")
        self.assertEqual((sent[4][1]["message_id"], sent[4][1]["text"]), (72, "ok"))

    def test_precheck_dedupe(self):
        calls = []

        async def fake_find(url):
            return "https://app.notion.com/p/existing"

        async def boom(*a, **k):
            calls.append("called")
            raise AssertionError("重复链接不应再抓取或调用 Gemini")
        self.app_module.find_existing = fake_find
        self.app_module.smart_fetch = boom
        self.app_module.analyze_content = boom
        msg = asyncio.run(self.app_module.archive_text("https://example.com/a?utm_source=x 批注", None))
        self.assertIn("已归档过", msg)
        self.assertEqual(calls, [])

    def test_health_and_channel_receipt(self):
        from types import SimpleNamespace
        os.environ["TAVILY_API_KEY"] = "tvly-test"
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("Tavily", body["channels"])
        self.assertNotIn("tvly-test", str(body))

        async def fake_find(url):
            return None

        async def fake_fetch(url):
            return SimpleNamespace(source_type="Web", clean_url=url, title="示例", content="正文" * 100,
                                   status="PASS", note="", channel="Tavily", fetch_tier="Tier2-Chain")

        async def fake_analyze(*a, **k):
            return SimpleNamespace(title="T", category="技术工具与开源", tags=["AI"], summary="摘要", key_points=["k"]), {}, "m"

        async def fake_save(*a, **k):
            return "https://notion.so/p/1", False

        self.app_module.find_existing = fake_find
        self.app_module.smart_fetch = fake_fetch
        self.app_module.analyze_content = fake_analyze
        self.app_module.save_to_notion = fake_save
        msg = asyncio.run(self.app_module.archive_text("https://example.com/post", None))
        self.assertIn("渠道：Tavily", msg)
        os.environ.pop("TAVILY_API_KEY", None)

    def test_batch_receipts_stay_linked_to_each_message(self):
        app = self.app_module

        async def run():
            sent = []

            async def telegram(method, payload):
                sent.append((method, payload))
                if method == "sendMessage":
                    return {"result": {"message_id": 100 + payload["reply_parameters"]["message_id"]}}
                return {"ok": True}

            async def archive(text, entities, message, media):
                await asyncio.sleep(0)
                return f"已归档 {message['message_id']}", "👌"

            with patch.object(app, "tg_call", telegram), patch.object(app, "_archive_message", archive):
                await asyncio.gather(*(app.process_update({"update_id": i, "message": {
                    "message_id": i, "chat": {"id": 5}, "from": {"id": 222}, "text": f"测试 {i}"}})
                    for i in range(1, 6)))
            return sent

        sent = asyncio.run(run())
        reactions = [(p["message_id"], p["reaction"][0]["emoji"]) for method, p in sent if method == "setMessageReaction"]
        self.assertEqual(reactions[:5], [(i, "👀") for i in range(1, 6)])
        self.assertCountEqual(reactions[5:], [(i, "👌") for i in range(1, 6)])
        self.assertCountEqual([(p["message_id"], p["text"]) for m, p in sent if m == "editMessageText"],
                              [(100 + i, f"已归档 {i}") for i in range(1, 6)])

    def test_caption_and_photo_reach_model_and_notion_together(self):
        app = self.app_module
        message = {"message_id": 9, "photo": [
            {"file_id": "high", "width": 200, "height": 200},
            {"file_id": "low", "width": 100, "height": 100}]}
        media = app.extract_media(message)
        result = app.AnalysisResult(title="图片", category="技术工具与开源", tags=["重要"], summary="识别结果",
                                    key_points=[], extracted_text="OCR")
        with patch.object(app, "_get_tg_client"), patch.object(app, "download_media", AsyncMock(return_value=b"photo")) as download, \
                patch.object(app, "analyze_content", AsyncMock(return_value=(result, {}, "mock"))) as analyze, \
                patch.object(app, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/1", False))) as save:
            receipt, reaction = asyncio.run(app._archive_message("批注 #重要", [], message, media))
        self.assertEqual(download.call_args.args[-1].file_id, "high")
        self.assertEqual(analyze.call_args.args[3], "批注 #重要")
        self.assertEqual(analyze.call_args.kwargs["media_bytes"], b"photo")
        self.assertEqual(analyze.call_args.kwargs["user_hashtags"], ["重要"])
        self.assertEqual(save.call_args.args[0].extracted_text, "OCR")
        self.assertEqual(save.call_args.kwargs["attachment"][0], b"photo")
        self.assertIn("Direct-Multimodal", receipt)
        self.assertEqual(reaction, "👌")

    def test_gemini_failure_archives_plain_text_as_pending(self):
        app = self.app_module
        text = "保留原始随手记 #重要"
        with patch.object(app, "analyze_content", AsyncMock(side_effect=RuntimeError)), \
                patch.object(app, "get_categories", AsyncMock(return_value=([{"name": "待整理"}], "mock"))), \
                patch.object(app, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/2", False))) as save:
            receipt, reaction = asyncio.run(app._archive_message(text, [], {}))
        self.assertEqual(save.call_args.args[3:5], ("AI_PENDING", text))
        self.assertEqual(save.call_args.args[0].tags, ["重要", "待整理"])
        self.assertTrue(save.call_args.kwargs["inbox"])
        self.assertEqual(reaction, "🤔")
        self.assertIn("AI_PENDING", receipt)

    def test_unreadable_link_preserves_original_and_every_failure(self):
        app = self.app_module
        url = "https://example.com/dead?utm_source=clip"
        fetched = SimpleNamespace(source_type="Web", clean_url="https://example.com/dead", title="", content="",
                                  status="NEEDS_GEMINI_WEB", note="Tavily HTTP 404", channel="", fetch_tier="")
        with patch.object(app, "_find_duplicate", AsyncMock(return_value=None)), \
                patch.object(app, "smart_fetch", AsyncMock(return_value=fetched)), \
                patch.object(app, "recover_web", AsyncMock(return_value=("", "", "FALLBACK", "URL Context 未读到"))), \
                patch.object(app, "get_categories", AsyncMock(return_value=([{"name": "待整理"}], "mock"))), \
                patch.object(app, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/3", False))) as save:
            _, reaction = asyncio.run(app._archive_message(f"{url} 我的批注", [], {}))
        self.assertEqual(save.call_args.args[5:7], ("我的批注", "Tavily HTTP 404；URL Context 未读到"))
        self.assertEqual(save.call_args.kwargs["original_url"], url)
        self.assertEqual(save.call_args.kwargs["fetch_tier"], "Tier4-Fallback")
        self.assertEqual(reaction, "🤔")


class TestGenericChainAndNotion(unittest.TestCase):
    def test_tavily_and_zenrows_parsers(self):
        import httpx
        import router

        def handler(req: httpx.Request) -> httpx.Response:
            if "api.tavily.com" in req.url.host:
                return httpx.Response(200, json={"results": [{"url": "https://a.com", "raw_content": "# 标题\n\n" + "内容" * 120}]})
            if "api.zenrows.com" in req.url.host:
                if "js_render" not in str(req.url):
                    return httpx.Response(200, text="<html><body><p>短</p></body></html>")
                return httpx.Response(200, text="<html><head><title>ZR标题</title></head><body><main><p>" + "动态正文" * 80 + "</p></main></body></html>")
            return httpx.Response(404)

        os.environ["TAVILY_API_KEY"] = "tvly-test"
        os.environ["ZENROWS_API_KEY"] = "zr-test"
        try:
            async def run():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    t_title, t_text = await router.fetch_via_tavily(client, "https://a.com")
                    z_title, z_text = await router.fetch_via_zenrows(client, "https://a.com")
                    return t_title, t_text, z_title, z_text

            t_title, t_text, z_title, z_text = asyncio.run(run())
            self.assertEqual(t_title, "标题")
            self.assertGreaterEqual(len(t_text), 200)
            self.assertEqual(z_title, "ZR标题")
            self.assertIn("动态正文", z_text)
        finally:
            os.environ.pop("TAVILY_API_KEY", None)
            os.environ.pop("ZENROWS_API_KEY", None)

    def test_generic_chain_priority_and_fallback(self):
        import httpx
        import router

        called = []

        async def ok_fn(name, text="A" * 300):
            called.append(name)
            return f"{name}-title", text

        async def short_fn(name):
            called.append(name)
            return "t", "short"

        orig = router._CHAIN_ORDER
        os.environ.update({"TAVILY_API_KEY": "1", "FIRECRAWL_API_KEY": "1", "ZENROWS_API_KEY": "1"})
        try:
            router._CHAIN_ORDER = [
                ("Tavily", lambda c, u: short_fn("Tavily")),
                ("Firecrawl", lambda c, u: ok_fn("Firecrawl")),
                ("ZenRows", lambda c, u: ok_fn("ZenRows")),
                ("Jina", lambda c, u: ok_fn("Jina")),
                ("直连", None),
            ]
            async def run():
                async with httpx.AsyncClient() as c:
                    return await router.fetch_generic_chain(c, "https://example.com")
            title, text, used, notes = asyncio.run(run())
            self.assertEqual(used, "Firecrawl")
            self.assertEqual(called, ["Tavily", "Firecrawl"])
            self.assertEqual(notes, ["Tavily正文过短"])
        finally:
            router._CHAIN_ORDER = orig
            for k in ("TAVILY_API_KEY", "FIRECRAWL_API_KEY", "ZENROWS_API_KEY"):
                os.environ.pop(k, None)

    def test_notion_loop_pool_and_retry(self):
        import httpx
        import notion_writer as nw

        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "0.01"}, text="rate limited")
            return httpx.Response(200, json={"ok": True})

        async def run_once():
            c1 = nw._get_client()
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_c:
                res = await nw._request(mock_c, "GET", "/users/me")
            return c1, res

        c1, res = asyncio.run(run_once())
        self.assertTrue(res["ok"])
        self.assertEqual(attempts["n"], 2)
        c2 = asyncio.run(nw.find_existing(""))  # 跨 asyncio.run() 不应报 Event loop is closed
        self.assertIsNone(c2)
        self.assertIsNotNone(c1)

    def test_v2ex_api_error_falls_back_to_generic_chain(self):
        import httpx
        import router

        def handler(req: httpx.Request) -> httpx.Response:
            if "v2ex.com/api/" in str(req.url) or "r.jina.ai" in req.url.host:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(200, text="<html><head><title>V2EX备选</title></head><body><main><p>" + "讨论" * 80 + "</p></main></body></html>")

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await router.fetch_v2ex(client, "https://www.v2ex.com/t/123456")

        r = asyncio.run(run())
        self.assertEqual(r.status, "PARTIAL")
        self.assertEqual(r.channel, "直连")
        self.assertIn("V2EX API 失败", r.note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
