import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import app
import router


class TestStructuredGitHub(unittest.IsolatedAsyncioTestCase):
    repo_url = "https://github.com/owner/repo"

    async def test_issue_paginates_and_prioritizes_maintainer_and_dissent(self):
        url = f"{self.repo_url}/issues/4"
        requests = []

        def comment(index, text, **fields):
            return {"html_url": f"{url}#issuecomment-{index}", "body": text, **fields}

        def handle(request):
            path = request.url.path
            requests.append((path, request.url.params.get("page")))
            if path == "/repos/owner/repo/issues/4":
                return httpx.Response(200, json={"title": "同步失败", "state": "open", "comments": 32,
                                                 "body": "现象和复现过程", "user": {"login": "reporter"}})
            self.assertEqual(path, "/repos/owner/repo/issues/4/comments")
            if request.url.params["page"] == "1":
                return httpx.Response(200, json=[comment(1, "me too")] + [
                    comment(index, f"普通跟进评论 {index}") for index in range(2, 31)
                ])
            return httpx.Response(200, json=[
                comment(31, "维护者建议暂时升级依赖", author_association="MEMBER"),
                comment(32, "It still fails in offline mode"),
            ])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await router.fetch_github(client, url)

        self.assertEqual(result.status, "PASS")
        self.assertEqual(requests[-2:], [("/repos/owner/repo/issues/4/comments", str(i)) for i in (1, 2)])
        self.assertIn("me too", result.content)
        self.assertNotIn("me too", result.model_content)
        self.assertIn("维护者建议", result.model_content)
        self.assertIn("still fails", result.model_content)
        self.assertIn(f"{url}#issuecomment-32", result.reference_links)
        self.assertLessEqual(len(result.reference_links), 12)

    async def test_pull_review_comment_rate_limit_keeps_description(self):
        def handle(request):
            if request.url.path == "/repos/owner/repo/pulls/5":
                return httpx.Response(200, json={"title": "更新", "state": "closed", "merged": True,
                                                 "body": "PR 正文", "review_comments": 2})
            self.assertEqual(request.url.path, "/repos/owner/repo/pulls/5/comments")
            return httpx.Response(429)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await router.fetch_github(client, f"{self.repo_url}/pull/5")
        self.assertEqual(result.status, "PARTIAL")
        self.assertIn("状态：merged", result.content)
        self.assertIn("PR 正文", result.content)
        self.assertIn("429", result.note)

    async def test_discussion_preserves_marked_answer_and_nested_replies(self):
        url = f"{self.repo_url}/discussions/6"
        answer = {"url": f"{url}#discussioncomment-2", "body": "这个做法可以绕开日常配置冲突"}
        first = {"url": f"{url}#discussioncomment-1", "body": "我也测试过这个场景",
                 "replies": {"totalCount": 1, "nodes": [answer]}}

        def handle(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.url.path, "/graphql")
            self.assertEqual(request.headers["authorization"], "Bearer test-token")
            kwargs = json.loads(request.content)
            self.assertEqual(kwargs["variables"], {"owner": "owner", "repo": "repo", "number": 6})
            return httpx.Response(200, json={"data": {"repository": {"discussion": {
                "title": "使用问题", "body": "讨论主贴", "isAnswered": True, "answer": answer,
                "comments": {"totalCount": 1, "nodes": [first]},
            }}}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch.object(router, "env", return_value="test-token"):
                result = await router.fetch_github(client, url)
        self.assertIn("状态：已标记答案", result.content)
        self.assertEqual(result.content.count(answer["body"]), 1)
        self.assertEqual(result.reference_links[0], answer["url"])

    async def test_release_tag_and_missing_discussion_credentials(self):
        def handle(request):
            self.assertIn(b"/releases/tags/feature%2F1.2", request.url.raw_path)
            return httpx.Response(200, json={"name": "功能版本", "tag_name": "feature/1.2", "body": "完整发布说明"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            release = await router.fetch_github(client, f"{self.repo_url}/releases/tag/feature/1.2")
            with patch.object(router, "env", return_value=""), self.assertRaisesRegex(ValueError, "GITHUB_TOKEN"):
                await router.fetch_github(client, f"{self.repo_url}/discussions/6")
        self.assertIn("版本：feature/1.2", release.content)
        self.assertIn("完整发布说明", release.content)

    async def test_webhook_archive_sends_selected_text_to_model_and_full_text_to_notion(self):
        url = f"{self.repo_url}/issues/4"
        links = (f"{url}#issuecomment-1",)
        fetched = router.FetchResult("GitHub", url, "示例", "保留抓取原文",
                                     model_content="筛选后的评论和主贴", reference_links=links)
        result = app.AnalysisResult(title="示例", category="待整理", tags=[], summary="摘要", key_points=[])
        with (patch.object(app, "_find_duplicate", AsyncMock(return_value=None)),
              patch.object(app, "smart_fetch", AsyncMock(return_value=fetched)),
              patch.object(app, "analyze_content", AsyncMock(return_value=(result, {}, "mock"))) as analyze,
              patch.object(app, "save_to_notion", AsyncMock(return_value=("https://notion.so/saved", False))) as save):
            await app._archive_message(url, [], {})
        self.assertEqual(analyze.call_args.args[2], fetched.model_content)
        self.assertEqual(save.call_args.args[4], fetched.content)
        self.assertEqual(save.call_args.kwargs["reference_links"], links)
