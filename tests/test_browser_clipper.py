import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_clipper as clipper
from notion_writer import _lookup_filter


class TestBrowserClip(unittest.IsolatedAsyncioTestCase):
    def test_pairing_is_scoped_signed_and_expiring(self):
        with patch.dict(os.environ, {"ALLOWED_USER_ID": "123", "BROWSER_CLIP_SECRET": "test-signing-key"}), \
                patch.object(clipper.time, "time", return_value=1800000000) as clock:
            self.assertIsNone(clipper.issue_clip_token(456))
            token = clipper.issue_clip_token(123)
            self.assertTrue(clipper.authorized_clip(f"Bearer {token}"))
            suffix = "a" if token[-1] != "a" else "b"
            self.assertFalse(clipper.authorized_clip(f"Bearer {token[:-1]}{suffix}"))
            self.assertFalse(clipper.authorized_clip("Bearer test-signing-key"))
            clock.return_value += clipper.TOKEN_LIFETIME
            self.assertFalse(clipper.authorized_clip(f"Bearer {token}"))

    def test_clip_accepts_http_sources_without_credentials(self):
        for url in ("javascript:alert(1)", "https://user:password@example.com", "file:///tmp/document"):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                clipper.BrowserClip(title="test", url=url, markdown="text", mode="page")

    async def test_archives_exact_selection_source_note_and_full_markdown_attachment(self):
        clip = clipper.BrowserClip(title="样例", url="https://example.org/article?a=1#part", mode="selection",
                                  markdown="只选中 **这一段**。", annotation="这段适用于后续工作。")
        result = SimpleNamespace(title="样例", summary="摘要", category="测试", tags=["参考"], key_points=[])
        with patch.object(clipper, "find_existing", AsyncMock(return_value=None)), \
                patch.object(clipper, "analyze_content", AsyncMock(return_value=(result, {}, "model"))) as analyze, \
                patch.object(clipper, "save_to_notion", AsyncMock(return_value=("https://notion.so/p/1", False))) as save:
            response = await clipper.archive_browser_clip(clip)
        self.assertEqual(response, {"ok": True, "url": "https://notion.so/p/1", "duplicate": False, "status": "PASS"})
        self.assertEqual(analyze.call_args.args[2:4], (clip.markdown, clip.annotation))
        self.assertEqual(save.call_args.args[2], clip.url)
        data, mime, filename = save.call_args.kwargs["attachment"]
        self.assertEqual((mime, filename), ("text/markdown", "样例.md"))
        self.assertIn(clip.url, data.decode())
        self.assertIn(clip.annotation, data.decode())
        self.assertIn(clip.markdown, data.decode())

    async def test_duplicate_reuses_notion_page_before_classification(self):
        clip = clipper.BrowserClip(title="题目", url="https://example.org/article", mode="page", markdown="一篇文章")
        with patch.object(clipper, "find_existing", AsyncMock(return_value="https://notion.so/p/existing")) as lookup, \
                patch.object(clipper, "analyze_content", AsyncMock()) as analyze:
            response = await clipper.archive_browser_clip(clip)
        self.assertTrue(response["duplicate"])
        self.assertEqual(len(lookup.call_args.kwargs["clip_key"]), 64)
        analyze.assert_not_called()

    def test_browser_deduplication_is_independent_of_telegram_url_deduplication(self):
        schema = {"URL": {"type": "url"}, "ClipKey": {"type": "rich_text"}}
        url = "https://example.org/article"
        self.assertEqual(_lookup_filter(schema, url, clip_key="key"), {"property": "ClipKey", "rich_text": {"equals": "key"}})
        self.assertIn({"property": "ClipKey", "rich_text": {"is_empty": True}}, _lookup_filter(schema, url)["and"])
