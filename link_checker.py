"""Incremental link-rot checker and safe local HTML snapshot archiver (#12)."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import httpx

from clip_store import ClipStore
from router import BROWSER_UA, UnsafeURL, _checked_url, _public_get

log = logging.getLogger("clipper")

SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "auth", "key", "api_key", "apikey", "secret",
    "signature", "sig", "session", "sid", "ticket", "password", "passwd", "pwd",
    "x-amz-signature", "x-amz-credential",
}


def is_public_archivable_url(url: str) -> bool:
    """严格过滤私密、内网、带凭证或临时签名参数的 URL，防止外传。"""
    if not url:
        return False
    try:
        _checked_url(url)
    except UnsafeURL:
        return False
    parsed = urlparse(url)
    for k, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if k.lower() in SENSITIVE_QUERY_KEYS:
            return False
    return True


def classify_http_status(status_code: int) -> str:
    if 200 <= status_code < 400:
        return "ALIVE"
    if status_code in (404, 410):
        return "DEAD"
    if status_code == 429 or status_code >= 500:
        return "TEMP_ERROR"
    return "RESTRICTED"


async def check_single_link(client: httpx.AsyncClient, url: str, *,
                            snapshot_dir: Path | None = None,
                            capture_snapshot: bool = False) -> tuple[str, str]:
    """返回 (link_status, snapshot_path)。仅对公开安全链接发起请求，不覆盖原文正文/状态。"""
    if not is_public_archivable_url(url):
        return "SKIPPED_PRIVATE", ""
    try:
        final_url, resp = await _public_get(
            client, url, {"User-Agent": BROWSER_UA}, 12.0, read_body=capture_snapshot,
        )
        status = classify_http_status(resp.status_code)
        snap_path = ""
        if status == "ALIVE" and capture_snapshot and snapshot_dir is not None:
            ctype = resp.headers.get("content-type", "").lower()
            if "html" in ctype or not ctype:
                snapshot_dir.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:16]
                target = snapshot_dir / f"{digest}.html"
                target.write_bytes(resp.content)
                snap_path = str(target)
        return status, snap_path
    except UnsafeURL:
        return "SKIPPED_PRIVATE", ""
    except (httpx.HTTPError, ValueError):
        return "TEMP_ERROR", ""


async def check_stale_links(store: ClipStore, *, tenant_id: int | None = None, stale_days: int = 30,
                            limit: int = 20, snapshot_dir: Path | str | None = None,
                            capture_snapshot: bool = False,
                            client: httpx.AsyncClient | None = None) -> list[dict]:
    items = store.clips_needing_link_check(tenant_id=tenant_id, stale_days=stale_days, limit=limit)
    if not items:
        return []
    snap_root = Path(snapshot_dir) if snapshot_dir is not None else None
    results = []
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(follow_redirects=False)
    try:
        for item in items:
            url = item["canonical_url"]
            want_snap = capture_snapshot and item.get("status") == "PASS"
            status, snap_path = await check_single_link(
                client, url, snapshot_dir=snap_root, capture_snapshot=want_snap,
            )
            store.update_link_status(item["id"], status, snapshot_path=snap_path)
            results.append({"clip_id": item["id"], "url": url, "link_status": status, "snapshot_path": snap_path})
    finally:
        if owns_client:
            await client.aclose()
    return results
