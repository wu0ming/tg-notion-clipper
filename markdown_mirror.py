"""One-way incremental Markdown mirror with YAML frontmatter for Obsidian/AnyTxt (#12)."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import yaml

from clip_store import ClipStore

SAFE_SLUG_RE = re.compile(r"[^\w\u4e00-\u9fff-]+")


def _slug(title: str) -> str:
    cleaned = SAFE_SLUG_RE.sub("-", (title or "").strip()).strip("-_")
    return cleaned[:48] or "clip"


def _format_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _render_markdown(clip: dict) -> str:
    frontmatter = {
        "id": clip["id"],
        "page_id": clip["page_id"],
        "title": clip["title"],
        "url": clip["canonical_url"],
        "notion_url": clip["page_url"],
        "source": clip["source"],
        "category": clip["category"],
        "entity_type": clip.get("entity_type") or "通用",
        "tags": clip.get("tags") or [],
        "status": clip["status"],
        "link_status": clip.get("link_status") or "UNCHECKED",
        "created_at": datetime.fromtimestamp(clip["created_at"], tz=timezone.utc).isoformat(),
        "updated_at": datetime.fromtimestamp(clip["updated_at"], tz=timezone.utc).isoformat(),
    }
    header = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    parts = [f"---\n{header}\n---", f"# {clip['title'] or '未命名'}"]
    if clip.get("summary"):
        parts.append(f"## 摘要\n\n{clip['summary']}")
    if clip.get("user_note"):
        parts.append(f"## 个人批注\n\n{clip['user_note']}")
    if clip.get("content_preview"):
        parts.append(f"## 正文摘录\n\n{clip['content_preview']}")
    return "\n\n".join(parts) + "\n"


def export_markdown_mirror(store: ClipStore, output_dir: Path | str, *, tenant_id: int = 0) -> dict:
    """增量导出指定用户的归档笔记到 Markdown 目录，保留稳定文件名与内容指纹。"""
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / ".mirror-manifest.json"
    manifest: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError:
            manifest = {}

    clips = store.list_recent_clips(tenant_id=tenant_id, limit=5000)
    written = 0
    skipped = 0
    for clip in clips:
        cid = clip["id"]
        markdown = _render_markdown(clip)
        digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        prev = manifest.get(cid)
        if prev and prev.get("sha256") == digest and (root / prev["filename"]).exists():
            skipped += 1
            continue
        filename = prev["filename"] if prev and prev.get("filename") else (
            f"{_format_iso(clip['created_at'])}-{_slug(clip['title'])}-{clip['page_id'][:8]}.md"
        )
        (root / filename).write_text(markdown, encoding="utf-8")
        manifest[cid] = {"filename": filename, "sha256": digest, "updated_at": clip["updated_at"]}
        written += 1

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"written": written, "skipped": skipped, "total": len(clips), "dir": str(root)}
