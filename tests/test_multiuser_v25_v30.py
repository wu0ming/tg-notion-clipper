import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app
import browser_clipper
import category_config
import config
import link_checker
import markdown_mirror
import mcp_server
import notion_writer
from clip_store import ClipStore
from sinks import NotionSink


class TestMultiUserV25AndV30(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "multi.db")
        self.master_key = Fernet.generate_key().decode("utf-8")
        self.enc_tok_b = "enc:" + Fernet(self.master_key.encode("utf-8")).encrypt(b"notion-secret-B").decode("utf-8")
        user_cfgs = {
            "111": {"archive_db": "db-a", "config_db": "cfg-a", "inbox_db": "inb-a", "notion_token": "tok-a", "monthly_budget_usd": 0.01, "quota_policy": "stop"},
            "222": {"archive_db": "db-b", "config_db": "cfg-b", "notion_token": self.enc_tok_b},
        }
        self.env_patch = patch.dict(os.environ, {
            "ALLOWED_USER_ID": "111,222",
            "USER_CONFIGS": json.dumps(user_cfgs),
            "CLIPPER_MASTER_KEY": self.master_key,
            "CLIPPER_DB_PATH": self.db_path,
            "WEBHOOK_SECRET": "wh-secret",
            "INGEST_SECRET": "ingest-secret",
            "NOTION_TOKEN": "admin-tok",
            "NOTION_DB_ARCHIVE": "admin-archive",
        })
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(self.tmpdir.cleanup)
        category_config._states.clear()

    def test_user_isolation_and_encrypted_credentials(self):
        ctx_a = config.resolve_user_context(111)
        ctx_b = config.resolve_user_context(222)
        self.assertEqual((ctx_a.archive_db, ctx_a.notion_token), ("db-a", "tok-a"))
        self.assertEqual((ctx_b.archive_db, ctx_b.notion_token), ("db-b", "notion-secret-B"))
        self.assertIsNone(config.resolve_user_context(999))  # 未知用户绝不回退至管理员库

        # 浏览器配对码按用户隔离
        tok_a = browser_clipper.issue_clip_token(111)
        tok_b = browser_clipper.issue_clip_token(222)
        self.assertEqual(browser_clipper.clip_token_user(f"Bearer {tok_a}"), 111)
        self.assertEqual(browser_clipper.clip_token_user(f"Bearer {tok_b}"), 222)
        self.assertIsNone(browser_clipper.issue_clip_token(999))

    async def test_two_users_same_url_isolated_in_notion_and_categories(self):
        created_dbs = []
        used_tokens = []

        def handle(request):
            used_tokens.append(request.headers["authorization"])
            path = request.url.path
            if path.startswith("/v1/databases/"):
                db = path.rsplit("/", 1)[-1]
                return httpx.Response(200, json={"data_sources": [{"id": f"ds-{db}"}]})
            if path.startswith("/v1/data_sources/") and request.method == "GET":
                return httpx.Response(200, json={"properties": {"Name": {"type": "title"}, "URL": {"type": "url"}}})
            if path.endswith("/query"):
                return httpx.Response(200, json={"results": []})
            body = json.loads(request.content)
            created_dbs.append(body["parent"]["data_source_id"])
            return httpx.Response(200, json={"id": f"page-{len(created_dbs)}", "url": f"https://notion.so/p/page-{len(created_dbs)}"})

        fetched = SimpleNamespace(source_type="Web", clean_url="https://example.org/shared", title="共享链接",
                                  content="正文内容", status="PASS", note="", channel="mock", fetch_tier="mock")
        analysis = app.AnalysisResult(title="共享", category="技术工具与开源", tags=["工具"], summary="摘要", key_points=[])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with (patch.object(notion_writer, "_ds_cache", {}),
                  patch.object(notion_writer, "_schema_cache", {}),
                  patch.object(notion_writer, "_get_client", return_value=client),
                  patch.object(app, "tg_call", AsyncMock(return_value={"result": {"message_id": 1}})),
                  patch.object(app, "smart_fetch", AsyncMock(return_value=fetched)),
                  patch.object(app, "analyze_content", AsyncMock(return_value=(analysis, {}, "m")))):
                await app.process_update({"update_id": 1, "message": {"message_id": 1, "chat": {"id": 111}, "from": {"id": 111}, "text": "https://example.org/shared"}})
                await app.process_update({"update_id": 2, "message": {"message_id": 2, "chat": {"id": 222}, "from": {"id": 222}, "text": "https://example.org/shared"}})

        self.assertEqual(created_dbs, ["ds-db-a", "ds-db-b"])
        self.assertIn("Bearer tok-a", used_tokens)
        self.assertIn("Bearer notion-secret-B", used_tokens)

    async def test_fts5_chinese_search_commands_callbacks_and_mcp(self):
        store = ClipStore(self.db_path)
        store.upsert_clip(
            page_id="page-101", page_url="https://notion.so/p/page-101", tenant_id=111,
            canonical_url="https://eur-lex.europa.eu/battery", title="欧盟电池护照 PPWR 合规指引",
            category="行业研报与专业洞察", tags=["ESG", "电池行业"], source="Web",
            status="PASS", summary="2027 年电池护照强制实施要求", content_preview="详细合规指标与碳足迹核算",
        )
        store.upsert_clip(
            page_id="page-202", page_url="https://notion.so/p/page-202", tenant_id=222,
            canonical_url="https://secret.example.com/private", title="用户222机密笔记",
            category="社媒观点与灵感", tags=["私密"], source="Note", status="PASS",
            summary="用户222专属", content_preview="不能被111搜索到",
        )

        # Telegram /find /recent /stats /retag
        replies = []

        async def fake_reply(chat_id, text, reply_to=None, reply_markup=None):
            replies.append(text)
            return 88

        with patch.object(app, "reply", side_effect=fake_reply), \
                patch.object(app, "update_page_tags", AsyncMock(return_value=["ESG", "合规"])):
            await app._process_message({"message": {"message_id": 10, "chat": {"id": 111}, "from": {"id": 111}, "text": "/find 电池护照"}})
            await app._process_message({"message": {"message_id": 11, "chat": {"id": 111}, "from": {"id": 111}, "text": "/recent 5"}})
            await app._process_message({"message": {"message_id": 12, "chat": {"id": 111}, "from": {"id": 111}, "text": "/retag page-101 ESG 合规"}})
            await app._process_message({"message": {"message_id": 13, "chat": {"id": 111}, "from": {"id": 111}, "text": "/stats"}})

        self.assertIn("page-101", replies[0])
        self.assertNotIn("page-202", replies[0])
        self.assertIn("page-101", replies[1])
        self.assertIn("合规", replies[2])
        self.assertIn("标签纠偏高频建议", replies[3])

        # MCP JSON-RPC 2.0 隔离测试
        list_resp = await mcp_server.handle_jsonrpc_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, store=store)
        self.assertEqual({t["name"] for t in list_resp["result"]["tools"]},
                         {"search_clips", "get_clip_content", "list_recent_clips", "append_clip_note"})
        denied = await mcp_server.handle_jsonrpc_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_clip_content", "arguments": {"page_id": "page-202", "user_id": 111}},
        }, store=store)
        self.assertTrue(denied["result"]["isError"])

        # Markdown 增量镜像
        mirror_dir = Path(self.tmpdir.name) / "mirror"
        res1 = markdown_mirror.export_markdown_mirror(store, mirror_dir, tenant_id=111)
        res2 = markdown_mirror.export_markdown_mirror(store, mirror_dir, tenant_id=111)
        self.assertEqual((res1["written"], res2["written"], res2["skipped"]), (1, 0, 1))

    async def test_link_checker_distinguishes_dead_temp_and_private(self):
        store = ClipStore(self.db_path)
        store.upsert_clip(page_id="p-alive", page_url="https://notion.so/p/1", tenant_id=111,
                          canonical_url="https://example.org/ok", title="OK", status="PASS")
        store.upsert_clip(page_id="p-dead", page_url="https://notion.so/p/2", tenant_id=111,
                          canonical_url="https://example.org/gone", title="Gone", status="PASS")
        store.upsert_clip(page_id="p-429", page_url="https://notion.so/p/3", tenant_id=111,
                          canonical_url="https://example.org/busy", title="Busy", status="PASS")
        store.upsert_clip(page_id="p-priv", page_url="https://notion.so/p/4", tenant_id=111,
                          canonical_url="https://example.org/doc?token=secret123", title="Priv", status="PASS")

        def handle(request):
            if request.url.path == "/ok":
                return httpx.Response(200, text="<html><body>Hello</body></html>", headers={"content-type": "text/html"})
            if request.url.path == "/gone":
                return httpx.Response(404)
            return httpx.Response(429)

        with patch.object(link_checker, "_public_get", AsyncMock(side_effect=lambda c, u, h, t, read_body=False: (u, handle(httpx.Request("GET", u))))):
            results = await link_checker.check_stale_links(
                store, tenant_id=111, stale_days=0, snapshot_dir=Path(self.tmpdir.name) / "snaps", capture_snapshot=True,
            )
        by_id = {r["clip_id"]: r for r in results}
        self.assertEqual(by_id["111:p-alive"]["link_status"], "ALIVE")
        self.assertTrue(Path(by_id["111:p-alive"]["snapshot_path"]).exists())
        self.assertEqual(by_id["111:p-dead"]["link_status"], "DEAD")
        self.assertEqual(by_id["111:p-429"]["link_status"], "TEMP_ERROR")
        self.assertEqual(by_id["111:p-priv"]["link_status"], "SKIPPED_PRIVATE")

    async def test_ingest_queue_retry_quota_and_100_block_batching(self):
        client = TestClient(app.app)
        resp = client.post(
            "/ingest",
            headers={"X-Ingest-Secret": "ingest-secret"},
            json={"url": "https://example.org/queued", "user_id": 111, "async_queue": True, "idempotency_key": "job-k1"},
        )
        self.assertEqual(resp.status_code, 200)
        job_id = resp.json()["job_id"]
        # 幂等入队
        resp_dup = client.post(
            "/ingest",
            headers={"X-Ingest-Secret": "ingest-secret"},
            json={"url": "https://example.org/queued", "user_id": 111, "async_queue": True, "idempotency_key": "job-k1"},
        )
        self.assertFalse(resp_dup.json()["created"])

        with patch.object(app, "_execute_ingest_payload", AsyncMock(side_effect=[RuntimeError("temp"), {"ok": True, "url": "https://notion.so/p/done"}])):
            r1 = await app.process_pending_jobs(limit=5)
            self.assertEqual(r1[0]["status"], "retry")
            store = ClipStore(self.db_path)
            with store._conn() as conn:
                conn.execute("UPDATE clip_jobs SET next_run_at=0 WHERE job_id=?", (job_id,))
            r2 = await app.process_pending_jobs(limit=5)
            self.assertEqual(r2[0]["status"], "done")

        # 超过 100 blocks 自动分片 + 租户预算停机策略
        store = ClipStore(self.db_path)
        store.log_usage(tenant_id=111, request_key="cost-1", model="gemini-3.8-flash", estimated_cost_usd=0.02)
        over, msg = app._budget_status(config.resolve_user_context(111))
        self.assertTrue(over)
        self.assertIn("已达上限", msg)

        batches = []

        def handle_notion(request):
            path = request.url.path
            if path == "/v1/databases/db-a":
                return httpx.Response(200, json={"data_sources": [{"id": "ds-a"}]})
            if path == "/v1/data_sources/ds-a":
                return httpx.Response(200, json={"properties": {"Name": {"type": "title"}}})
            if path == "/v1/pages":
                batches.append(("POST", len(json.loads(request.content)["children"])))
                return httpx.Response(200, json={"id": "big-page", "url": "https://notion.so/p/big-page"})
            if path == "/v1/blocks/big-page/children":
                batches.append(("PATCH", len(json.loads(request.content)["children"])))
                return httpx.Response(200, json={"results": []})
            return httpx.Response(404)

        sink = NotionSink(scope=config.resolve_user_context(111).notion_scope())
        extra_blocks = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"b{i}"}}]}}
                        for i in range(120)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle_notion)) as hc:
            with (patch.object(notion_writer, "_ds_cache", {}),
                  patch.object(notion_writer, "_schema_cache", {}),
                  patch.object(notion_writer, "_get_client", return_value=hc)):
                url, dup = await sink.create_item({
                    "result": app.AnalysisResult(title="超长块测试", category="技术", tags=[], summary="摘要", key_points=["1"]),
                    "source_type": "Web", "url": "", "status": "PASS", "raw_content": "正文",
                    "extra_blocks": extra_blocks,
                })
        self.assertFalse(dup)
        self.assertEqual(url, "https://notion.so/p/big-page")
        self.assertEqual(batches[0], ("POST", 100))
        self.assertGreaterEqual(batches[1][1], 20)

    async def test_callback_query_auth_and_idempotency(self):
        store = ClipStore(self.db_path)
        store.upsert_clip(
            page_id="page-cb", page_url="https://notion.so/p/page-cb", tenant_id=111,
            canonical_url="https://example.org/cb", title="回调条目", category="技术工具与开源",
            tags=["AI"], source="Web", status="PASS",
        )
        cb_calls = []

        async def fake_tg(method, payload):
            cb_calls.append((method, payload))
            return {"ok": True}

        analysis = app.AnalysisResult(title="深度精读", category="技术工具与开源", tags=["AI", "精读"],
                                      summary="深度总结", key_points=["要点1"])
        with (patch.object(app, "tg_call", side_effect=fake_tg),
              patch.object(app, "reply", AsyncMock(return_value=99)) as reply_mock,
              patch.object(app, "fetch_page_content", AsyncMock(return_value={"text": "原正文"})),
              patch.object(app, "analyze_content", AsyncMock(return_value=(analysis, {}, "gemini-3.8-flash"))) as analyze,
              patch.object(app, "enrich_notion_page", AsyncMock(return_value=("https://notion.so/p/page-cb", False)))):
            # 未授权用户被拒绝
            await app.process_update({"callback_query": {"id": "cb-0", "from": {"id": 999}, "data": "deep:page-cb", "message": {"chat": {"id": 999}, "message_id": 1}}})
            analyze.assert_not_awaited()
            # 同一 callback_id 重复投递幂等不重复调用模型
            cb_payload = {"callback_query": {"id": "cb-1", "from": {"id": 111}, "data": "deep:page-cb", "message": {"chat": {"id": 111}, "message_id": 2}}}
            await app.process_update(cb_payload)
            await app.process_update(cb_payload)
            analyze.assert_awaited_once()
            self.assertIn("深度精读", reply_mock.call_args.args[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
