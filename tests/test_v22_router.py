import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import router
from utils import detect_source, normalize_url


def encoded_file(content: str) -> dict:
    return {"type": "file", "encoding": "base64", "content": base64.b64encode(content.encode()).decode()}


class TestGitHub(unittest.IsolatedAsyncioTestCase):
    async def test_repository_metadata_and_readme(self):
        client_class = httpx.AsyncClient

        def handler(request):
            self.assertEqual(request.url.host, "api.github.com")
            self.assertEqual(request.headers["authorization"], "Bearer test-github-token")
            if request.url.path == "/repos/owner/repo":
                return httpx.Response(200, json={"full_name": "owner/repo", "description": "示例",
                                                 "stargazers_count": 5, "forks_count": 2, "language": "Python",
                                                 "topics": ["demo"], "license": {"spdx_id": "MIT"}})
            self.assertEqual(request.url.path, "/repos/owner/repo/readme")
            return httpx.Response(200, json=encoded_file("# 项目说明"))

        with patch.object(router, "env", return_value="test-github-token"), patch.object(
            router.httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(handler), **kwargs)
        ):
            result = await router.smart_fetch("https://www.github.com/owner/repo?utm_source=telegram")
        self.assertEqual((result.source_type, result.fetch_tier, result.status), ("GitHub", "Tier1-API", "PASS"))
        self.assertEqual(result.clean_url, "https://github.com/owner/repo")
        for fragment in ("# 项目说明", "Stars：5", "Forks：2", "Python", "demo", "MIT"):
            self.assertIn(fragment, result.content)

    async def test_tree_and_blob_keep_original_target(self):
        call_paths = []

        def handler(request):
            call_paths.append((request.url.path, request.url.params["ref"]))
            path, ref = call_paths[-1]
            if ref == "feature":
                return httpx.Response(404)
            if path == "/repos/owner/repo/contents/docs":
                return httpx.Response(200, json=[{"type": "file", "name": "使用指南.md"}])
            self.assertEqual(path, "/repos/owner/repo/contents/docs/使用指南.md")
            return httpx.Response(200, json=encoded_file("# 文件原文"))

        tree = "https://github.com/owner/repo/tree/feature/demo/docs"
        blob = "https://github.com/owner/repo/blob/feature/demo/docs/%E4%BD%BF%E7%94%A8%E6%8C%87%E5%8D%97.md#L1-L2"
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            tree_result = await router.fetch_github(client, tree)
            blob_result = await router.fetch_github(client, normalize_url(blob))
        self.assertEqual(tree_result.clean_url, tree)
        self.assertIn("使用指南.md", tree_result.content)
        self.assertEqual(blob_result.clean_url, blob)
        self.assertIn("文件原文", blob_result.content)
        self.assertIn(("/repos/owner/repo/contents/docs/使用指南.md", "feature/demo"), call_paths)

    async def test_issue_and_pull_use_detail_api(self):
        def handler(request):
            self.assertIn(request.url.path, {"/repos/owner/repo/issues/4", "/repos/owner/repo/pulls/5"})
            return httpx.Response(200, json={"title": "报告", "state": "closed", "body": "详情内容",
                                             "merged": request.url.path.endswith("/5"), "user": {"login": "alice"}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            for kind, number, state in (("issues", 4, "closed"), ("pull", 5, "merged")):
                result = await router.fetch_github(client, f"https://github.com/owner/repo/{kind}/{number}")
                self.assertEqual(result.fetch_tier, "Tier1-API")
                self.assertIn(f"状态：{state}", result.content)
                self.assertIn("详情内容", result.content)

    async def test_tier1_failure_preserves_source_and_failure_reason(self):
        client_class = httpx.AsyncClient
        with patch.object(router.httpx, "AsyncClient", lambda **kwargs: client_class(
            transport=httpx.MockTransport(lambda request: httpx.Response(404)), **kwargs
        )), patch.object(router, "_CHAIN_ORDER", []):
            result = await router.smart_fetch("https://github.com/owner/missing")
        self.assertEqual((result.source_type, result.status), ("GitHub", "NEEDS_GEMINI_WEB"))
        self.assertIn("GitHub API 失败：HTTP 404", result.note)

    async def test_github_token_is_not_forwarded_on_redirect(self):
        seen_hosts = []

        def handler(request):
            seen_hosts.append(request.url.host)
            self.assertEqual(request.headers["authorization"], "Bearer test-github-token")
            return httpx.Response(302, headers={"location": "https://example.com/elsewhere"})

        with patch.object(router, "env", return_value="test-github-token"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                with self.assertRaises(httpx.HTTPStatusError):
                    await router.fetch_github(client, "https://github.com/owner/repo")
        self.assertEqual(seen_hosts, ["api.github.com"])


class TestTier2(unittest.IsolatedAsyncioTestCase):
    async def test_quality_gate_rejects_long_challenge(self):
        blocked = AsyncMock(return_value=("", "# Just a moment…\n\n" + "Please wait. " * 100))
        valid = AsyncMock(return_value=("正题", "可核对的正文" * 50))
        with patch.object(router, "_CHAIN_ORDER", [("Tavily", blocked), ("Firecrawl", valid)]), patch.object(
            router, "env", return_value="mock-key"
        ):
            async with httpx.AsyncClient() as client:
                result = await router.fetch_web(client, "https://example.com/post")
        self.assertEqual((result.fetch_tier, result.channel, result.status), ("Tier2-Chain", "Firecrawl", "PASS"))
        self.assertIn("验证、拦截或错误页", result.note)

    async def test_failure_notes_exclude_credentials(self):
        request = httpx.Request("GET", "https://api.zenrows.com/v1/?apikey=mock-private-key")
        response = httpx.Response(403, request=request)
        denied = AsyncMock(side_effect=httpx.HTTPStatusError("mock-private-key", request=request, response=response))
        with patch.object(router, "_CHAIN_ORDER", [("ZenRows", denied)]), patch.object(router, "env", return_value="x"):
            async with httpx.AsyncClient() as client:
                result = await router.fetch_web(client, "https://example.com/post")
        self.assertEqual(result.status, "NEEDS_GEMINI_WEB")
        self.assertEqual(result.note, "ZenRows失败：HTTP 403")

    async def test_metadata_and_internal_url_are_blocked_before_any_fetch(self):
        for address in ("http://169.254.169.254/", "https://[::1]/", "https://metadata.google.internal/",
                        "https://name:secret@example.com/", "https://example.com:8000/"):
            with self.subTest(address=address):
                result = await router.smart_fetch(address)
                self.assertEqual((result.status, result.clean_url), ("FALLBACK", ""))

    async def test_direct_redirect_is_checked_and_uses_validated_ip(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

        with patch.object(router, "_pinned_address", AsyncMock(return_value="93.184.215.14")):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
                with self.assertRaises(router.UnsafeURL):
                    await router.fetch_direct_html(client, "https://example.com/article")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.host, "93.184.215.14")
        self.assertEqual(calls[0].headers["host"], "example.com")
        self.assertEqual(calls[0].extensions["sni_hostname"], "example.com")

    def test_platform_domain_boundaries(self):
        self.assertEqual(detect_source("https://www.github.com/owner/repo"), "GitHub")
        self.assertEqual(detect_source("https://fakev2ex.com/t/1"), "Web")
        self.assertEqual(detect_source("https://fakereddit.com/r/example"), "Web")


if __name__ == "__main__":
    unittest.main()
