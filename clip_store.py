"""Lightweight SQLite WAL store for indexing, FTS5 search, album state, usage quotas, and job queue."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from config import BASE_DIR, env

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_+#.-]+")


def default_db_path() -> Path:
    custom = env("CLIPPER_DB_PATH")
    if custom:
        return Path(custom)
    if os.environ.get("K_SERVICE"):
        return Path("/tmp/tg_notion_clipper.db")
    return BASE_DIR / "data" / "clipper.db"


def fts_index_text(text: str) -> str:
    """Emit standard tokens plus CJK character bigrams/unigrams so SQLite unicode61 matches Chinese queries."""
    text = text or ""
    tokens: list[str] = []
    tokens.extend(m.group(0).lower() for m in _WORD_RE.finditer(text))
    for run in _CJK_RUN_RE.findall(text):
        tokens.append(run)
        chars = list(run)
        if len(chars) == 1:
            tokens.append(chars[0])
        else:
            tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
            if len(chars) <= 4:
                tokens.extend(chars)
    return " ".join(tokens)


def fts_match_query(query: str) -> str:
    """Convert user search text into an FTS5 MATCH expression."""
    terms: list[str] = []
    for word in _WORD_RE.findall(query):
        cleaned = word.replace('"', "")
        if cleaned:
            terms.append(f'"{cleaned.lower()}"')
    for run in _CJK_RUN_RE.findall(query):
        if len(run) <= 2:
            terms.append(f'"{run}"')
        else:
            bigrams = [f'"{run[i]}{run[i + 1]}"' for i in range(len(run) - 1)]
            terms.append("(" + " AND ".join(bigrams) + ")")
    return " AND ".join(terms)


class ClipStore:
    """Thread-safe SQLite WAL backing store shared across CLI, webhook, MCP, and worker flows."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    @contextmanager
    def _conn(self):
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=15.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS clips (
                    id TEXT PRIMARY KEY,
                    tenant_id INTEGER NOT NULL DEFAULT 0,
                    page_id TEXT NOT NULL,
                    page_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL DEFAULT '',
                    url_hash TEXT NOT NULL DEFAULT '',
                    project_key TEXT NOT NULL DEFAULT '',
                    telegram_key TEXT NOT NULL DEFAULT '',
                    album_key TEXT NOT NULL DEFAULT '',
                    clip_key TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    entity_type TEXT NOT NULL DEFAULT '通用',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'PASS',
                    fetch_tier TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    content_preview TEXT NOT NULL DEFAULT '',
                    user_note TEXT NOT NULL DEFAULT '',
                    link_status TEXT NOT NULL DEFAULT 'UNCHECKED',
                    last_checked_at REAL,
                    snapshot_path TEXT NOT NULL DEFAULT '',
                    wayback_url TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_clips_tenant_url ON clips(tenant_id, canonical_url);
                CREATE INDEX IF NOT EXISTS idx_clips_tenant_proj ON clips(tenant_id, project_key);
                CREATE INDEX IF NOT EXISTS idx_clips_tenant_tg ON clips(tenant_id, telegram_key);
                CREATE INDEX IF NOT EXISTS idx_clips_tenant_album ON clips(tenant_id, album_key);
                CREATE INDEX IF NOT EXISTS idx_clips_tenant_updated ON clips(tenant_id, updated_at DESC);

                CREATE VIRTUAL TABLE IF NOT EXISTS clips_fts USING fts5(
                    clip_id UNINDEXED,
                    tenant_id UNINDEXED,
                    title_idx,
                    summary_idx,
                    tags_idx,
                    category_idx,
                    content_idx,
                    tokenize='unicode61'
                );

                CREATE TABLE IF NOT EXISTS album_members (
                    album_key TEXT NOT NULL,
                    tenant_id INTEGER NOT NULL DEFAULT 0,
                    message_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    unit_index INTEGER,
                    page_url TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (album_key, message_id)
                );

                CREATE TABLE IF NOT EXISTS album_units (
                    album_key TEXT NOT NULL,
                    unit_index INTEGER NOT NULL,
                    tenant_id INTEGER NOT NULL DEFAULT 0,
                    telegram_key TEXT NOT NULL,
                    page_url TEXT NOT NULL,
                    member_ids_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (album_key, unit_index)
                );

                CREATE TABLE IF NOT EXISTS usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id INTEGER NOT NULL DEFAULT 0,
                    request_key TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL,
                    route_tier TEXT NOT NULL DEFAULT '',
                    service_tier TEXT NOT NULL DEFAULT 'standard',
                    url_context INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_tenant_time ON usage_logs(tenant_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS corrections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id INTEGER NOT NULL DEFAULT 0,
                    clip_id TEXT NOT NULL,
                    domain TEXT NOT NULL DEFAULT '',
                    title_keywords TEXT NOT NULL DEFAULT '',
                    old_tags_json TEXT NOT NULL DEFAULT '[]',
                    new_tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS callback_actions (
                    callback_id TEXT PRIMARY KEY,
                    tenant_id INTEGER NOT NULL DEFAULT 0,
                    action TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    result_text TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clip_jobs (
                    job_id TEXT PRIMARY KEY,
                    tenant_id INTEGER NOT NULL DEFAULT 0,
                    idempotency_key TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    canonical_url TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    next_run_at REAL NOT NULL,
                    fetch_tier TEXT NOT NULL DEFAULT '',
                    page_url TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_tenant_idem ON clip_jobs(tenant_id, idempotency_key);
                CREATE INDEX IF NOT EXISTS idx_jobs_status_run ON clip_jobs(status, next_run_at);

                CREATE TABLE IF NOT EXISTS video_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)

    # ---------- Clips & FTS5 ----------
    def upsert_clip(self, *, page_id: str, page_url: str, tenant_id: int = 0, canonical_url: str = "",
                    url_hash: str = "", project_key: str = "", telegram_key: str = "", album_key: str = "",
                    clip_key: str = "", title: str = "", category: str = "", entity_type: str = "通用",
                    tags: list[str] | None = None, source: str = "", status: str = "PASS",
                    fetch_tier: str = "", summary: str = "", content_preview: str = "",
                    user_note: str = "") -> str:
        now = time.time()
        clip_id = f"{tenant_id}:{page_id}"
        tags_list = list(tags or [])
        tags_json = json.dumps(tags_list, ensure_ascii=False)
        with self._conn() as conn:
            existing = conn.execute("SELECT created_at FROM clips WHERE id=?", (clip_id,)).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute("""
                INSERT INTO clips (
                    id, tenant_id, page_id, page_url, canonical_url, url_hash, project_key,
                    telegram_key, album_key, clip_key, title, category, entity_type, tags_json,
                    source, status, fetch_tier, summary, content_preview, user_note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    page_url=excluded.page_url,
                    canonical_url=COALESCE(NULLIF(excluded.canonical_url, ''), clips.canonical_url),
                    url_hash=COALESCE(NULLIF(excluded.url_hash, ''), clips.url_hash),
                    project_key=COALESCE(NULLIF(excluded.project_key, ''), clips.project_key),
                    telegram_key=COALESCE(NULLIF(excluded.telegram_key, ''), clips.telegram_key),
                    album_key=COALESCE(NULLIF(excluded.album_key, ''), clips.album_key),
                    clip_key=COALESCE(NULLIF(excluded.clip_key, ''), clips.clip_key),
                    title=COALESCE(NULLIF(excluded.title, ''), clips.title),
                    category=COALESCE(NULLIF(excluded.category, ''), clips.category),
                    entity_type=COALESCE(NULLIF(excluded.entity_type, ''), clips.entity_type),
                    tags_json=excluded.tags_json,
                    source=COALESCE(NULLIF(excluded.source, ''), clips.source),
                    status=excluded.status,
                    fetch_tier=COALESCE(NULLIF(excluded.fetch_tier, ''), clips.fetch_tier),
                    summary=COALESCE(NULLIF(excluded.summary, ''), clips.summary),
                    content_preview=COALESCE(NULLIF(excluded.content_preview, ''), clips.content_preview),
                    user_note=COALESCE(NULLIF(excluded.user_note, ''), clips.user_note),
                    updated_at=excluded.updated_at
            """, (clip_id, tenant_id, page_id, page_url, canonical_url, url_hash, project_key,
                  telegram_key, album_key, clip_key, title, category, entity_type or "通用", tags_json,
                  source, status, fetch_tier, summary, content_preview[:12000], user_note[:4000],
                  created_at, now))
            conn.execute("DELETE FROM clips_fts WHERE clip_id=?", (clip_id,))
            conn.execute("""
                INSERT INTO clips_fts (clip_id, tenant_id, title_idx, summary_idx, tags_idx, category_idx, content_idx)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (clip_id, str(tenant_id), fts_index_text(title), fts_index_text(summary),
                  fts_index_text(" ".join(tags_list)), fts_index_text(category),
                  fts_index_text(f"{user_note}\n{content_preview[:8000]}")))
        return clip_id

    @staticmethod
    def _row_to_clip(row: sqlite3.Row | None) -> dict | None:
        if not row:
            return None
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json", "[]") or "[]")
        return item

    def get_clip(self, clip_or_page_id: str, tenant_id: int = 0) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM clips WHERE tenant_id=? AND (id=? OR page_id=?)",
                (tenant_id, clip_or_page_id, clip_or_page_id),
            ).fetchone()
            return self._row_to_clip(row)

    def find_match(self, *, tenant_id: int = 0, canonical_url: str = "", project_key: str = "",
                   telegram_key: str = "", clip_key: str = "") -> dict | None:
        with self._conn() as conn:
            for col, val in (("clip_key", clip_key), ("telegram_key", telegram_key),
                             ("canonical_url", canonical_url), ("project_key", project_key)):
                if not val:
                    continue
                row = conn.execute(
                    f"SELECT * FROM clips WHERE tenant_id=? AND {col}=? ORDER BY updated_at DESC LIMIT 1",
                    (tenant_id, val),
                ).fetchone()
                if row:
                    return self._row_to_clip(row)
        return None

    def search_clips(self, query: str, *, tenant_id: int = 0, theme: str = "",
                     tags: list[str] | None = None, limit: int = 5) -> list[dict]:
        match_expr = fts_match_query(query)
        with self._conn() as conn:
            if match_expr:
                rows = conn.execute("""
                    SELECT c.* FROM clips_fts f
                    JOIN clips c ON c.id = f.clip_id
                    WHERE f.tenant_id = ? AND clips_fts MATCH ?
                    ORDER BY bm25(clips_fts, 4.0, 2.0, 3.0, 1.5, 1.0), c.updated_at DESC
                    LIMIT ?
                """, (str(tenant_id), match_expr, max(limit * 4, 20))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM clips WHERE tenant_id=? ORDER BY updated_at DESC LIMIT ?",
                    (tenant_id, max(limit * 4, 20)),
                ).fetchall()
        results = []
        req_tags = {t.strip().casefold() for t in (tags or []) if t.strip()}
        for row in rows:
            item = self._row_to_clip(row)
            if theme and item["category"] != theme:
                continue
            if req_tags and not (req_tags & {t.casefold() for t in item["tags"]}):
                continue
            results.append(item)
            if len(results) >= limit:
                break
        return results

    def list_recent_clips(self, *, tenant_id: int = 0, days: int | None = None, limit: int = 10) -> list[dict]:
        cutoff = time.time() - days * 86400 if days else 0.0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM clips WHERE tenant_id=? AND updated_at>=? ORDER BY updated_at DESC LIMIT ?",
                (tenant_id, cutoff, limit),
            ).fetchall()
        return [self._row_to_clip(row) for row in rows]

    # ---------- Album State ----------
    def record_album_member(self, album_key: str, tenant_id: int, message_id: int, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT OR IGNORE INTO album_members (album_key, tenant_id, message_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (album_key, tenant_id, message_id, json.dumps(payload, ensure_ascii=False), time.time()))

    def save_album_unit(self, album_key: str, unit_index: int, tenant_id: int,
                        telegram_key: str, page_url: str, member_ids: list[int]) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO album_units (album_key, unit_index, tenant_id, telegram_key, page_url, member_ids_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(album_key, unit_index) DO UPDATE SET
                    page_url=excluded.page_url,
                    member_ids_json=excluded.member_ids_json
            """, (album_key, unit_index, tenant_id, telegram_key, page_url, json.dumps(member_ids), time.time()))
            for mid in member_ids:
                conn.execute(
                    "UPDATE album_members SET unit_index=?, page_url=? WHERE album_key=? AND message_id=?",
                    (unit_index, page_url, album_key, mid),
                )

    def get_album_units(self, album_key: str, tenant_id: int) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM album_units WHERE album_key=? AND tenant_id=? ORDER BY unit_index ASC",
                (album_key, tenant_id),
            ).fetchall()
        return [{**dict(r), "member_ids": json.loads(r["member_ids_json"])} for r in rows]

    def get_album_member_page(self, album_key: str, tenant_id: int, message_id: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT page_url FROM album_members WHERE album_key=? AND tenant_id=? AND message_id=?",
                (album_key, tenant_id, message_id),
            ).fetchone()
            return row["page_url"] if row and row["page_url"] else None

    # ---------- Usage, Quotas & Stats ----------
    def log_usage(self, *, tenant_id: int = 0, request_key: str = "", model: str, route_tier: str = "",
                  service_tier: str = "standard", url_context: bool = False, prompt_tokens: int = 0,
                  output_tokens: int = 0, thinking_tokens: int = 0, cached_tokens: int = 0,
                  estimated_cost_usd: float = 0.0, duration_ms: int = 0, error: str | None = None) -> None:
        with self._conn() as conn:
            if request_key:
                exists = conn.execute(
                    "SELECT 1 FROM usage_logs WHERE tenant_id=? AND request_key=? AND error IS NULL LIMIT 1",
                    (tenant_id, request_key),
                ).fetchone()
                if exists and error is None:
                    return
            conn.execute("""
                INSERT INTO usage_logs (
                    tenant_id, request_key, model, route_tier, service_tier, url_context,
                    prompt_tokens, output_tokens, thinking_tokens, cached_tokens,
                    estimated_cost_usd, duration_ms, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, request_key, model, route_tier, service_tier, int(url_context),
                  prompt_tokens or 0, output_tokens or 0, thinking_tokens or 0, cached_tokens or 0,
                  estimated_cost_usd or 0.0, duration_ms or 0, error, time.time()))

    def monthly_spend(self, tenant_id: int = 0, now: float | None = None) -> float:
        now = now or time.time()
        lt = time.gmtime(now)
        month_start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, 0))
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS total FROM usage_logs WHERE tenant_id=? AND created_at>=?",
                (tenant_id, month_start),
            ).fetchone()
        return float(row["total"] if row else 0.0)

    def stats(self, *, tenant_id: int = 0, days: int = 7) -> dict:
        cutoff = time.time() - days * 86400
        with self._conn() as conn:
            total_clips = conn.execute("SELECT COUNT(*) FROM clips WHERE tenant_id=?", (tenant_id,)).fetchone()[0]
            recent_clips = conn.execute(
                "SELECT COUNT(*) FROM clips WHERE tenant_id=? AND created_at>=?", (tenant_id, cutoff)
            ).fetchone()[0]
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS cnt FROM clips WHERE tenant_id=? AND created_at>=? GROUP BY status",
                (tenant_id, cutoff),
            ).fetchall()
            tier_rows = conn.execute(
                "SELECT fetch_tier, COUNT(*) AS cnt FROM clips WHERE tenant_id=? AND created_at>=? GROUP BY fetch_tier",
                (tenant_id, cutoff),
            ).fetchall()
            usage = conn.execute("""
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                       COALESCE(SUM(output_tokens), 0) AS output_tokens,
                       COALESCE(SUM(thinking_tokens), 0) AS thinking_tokens,
                       COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                       COALESCE(SUM(estimated_cost_usd), 0.0) AS cost_usd
                FROM usage_logs WHERE tenant_id=? AND created_at>=?
            """, (tenant_id, cutoff)).fetchone()
            corr_rows = conn.execute(
                "SELECT domain, old_tags_json, new_tags_json FROM corrections WHERE tenant_id=? ORDER BY id DESC LIMIT 50",
                (tenant_id,),
            ).fetchall()
        tag_shifts: dict[tuple[str, str], int] = {}
        for row in corr_rows:
            old = set(json.loads(row["old_tags_json"]))
            new = set(json.loads(row["new_tags_json"]))
            for added in new - old:
                key = (row["domain"] or "通用", added)
                tag_shifts[key] = tag_shifts.get(key, 0) + 1
        suggestions = [f"{dom} → +{tag} ({cnt}次)" for (dom, tag), cnt in
                       sorted(tag_shifts.items(), key=lambda kv: kv[1], reverse=True)[:3]]
        return {
            "total_clips": total_clips,
            "recent_clips": recent_clips,
            "days": days,
            "by_status": {r["status"]: r["cnt"] for r in status_rows},
            "by_tier": {(r["fetch_tier"] or "Default"): r["cnt"] for r in tier_rows},
            "usage": dict(usage) if usage else {},
            "tag_suggestions": suggestions,
        }

    # ---------- Corrections & Callbacks ----------
    def record_correction(self, *, tenant_id: int, clip_id: str, url: str, title: str,
                          old_tags: list[str], new_tags: list[str]) -> None:
        domain = urlparse(url).hostname or "" if url else ""
        keywords = " ".join(re.findall(r"[\w\u4e00-\u9fff]{2,}", title)[:8])
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO corrections (tenant_id, clip_id, domain, title_keywords, old_tags_json, new_tags_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, clip_id, domain, keywords, json.dumps(old_tags, ensure_ascii=False),
                  json.dumps(new_tags, ensure_ascii=False), time.time()))
            conn.execute("UPDATE clips SET tags_json=?, updated_at=? WHERE tenant_id=? AND (id=? OR page_id=?)",
                         (json.dumps(new_tags, ensure_ascii=False), time.time(), tenant_id, clip_id, clip_id))

    def claim_callback(self, callback_id: str, tenant_id: int, action: str, target_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO callback_actions (callback_id, tenant_id, action, target_id, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (callback_id, tenant_id, action, target_id, time.time()))
            return cur.rowcount == 1

    def finish_callback(self, callback_id: str, result_text: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE callback_actions SET result_text=? WHERE callback_id=?", (result_text, callback_id))

    # ---------- Link Checking ----------
    def clips_needing_link_check(self, *, tenant_id: int | None = None, stale_days: int = 30, limit: int = 20) -> list[dict]:
        cutoff = time.time() - stale_days * 86400
        with self._conn() as conn:
            if tenant_id is None:
                rows = conn.execute("""
                    SELECT * FROM clips
                    WHERE canonical_url != '' AND (last_checked_at IS NULL OR last_checked_at < ?)
                    ORDER BY COALESCE(last_checked_at, 0) ASC LIMIT ?
                """, (cutoff, limit)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM clips
                    WHERE tenant_id=? AND canonical_url != '' AND (last_checked_at IS NULL OR last_checked_at < ?)
                    ORDER BY COALESCE(last_checked_at, 0) ASC LIMIT ?
                """, (tenant_id, cutoff, limit)).fetchall()
        return [self._row_to_clip(r) for r in rows]

    def update_link_status(self, clip_id: str, link_status: str, *, snapshot_path: str = "",
                           wayback_url: str = "") -> None:
        now = time.time()
        with self._conn() as conn:
            conn.execute("""
                UPDATE clips SET link_status=?, last_checked_at=?,
                    snapshot_path=COALESCE(NULLIF(?, ''), snapshot_path),
                    wayback_url=COALESCE(NULLIF(?, ''), wayback_url),
                    updated_at=?
                WHERE id=?
            """, (link_status, now, snapshot_path, wayback_url, now, clip_id))

    # ---------- Persistent Job Queue (#13) ----------
    def enqueue_job(self, *, tenant_id: int, idempotency_key: str, source_type: str,
                    canonical_url: str, payload: dict, max_attempts: int = 3) -> tuple[dict, bool]:
        now = time.time()
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM clip_jobs WHERE tenant_id=? AND idempotency_key=?",
                (tenant_id, idempotency_key),
            ).fetchone()
            if existing:
                return dict(existing), False
            job_id = uuid.uuid4().hex[:16]
            conn.execute("""
                INSERT INTO clip_jobs (
                    job_id, tenant_id, idempotency_key, source_type, canonical_url, payload_json,
                    status, attempts, max_attempts, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
            """, (job_id, tenant_id, idempotency_key, source_type, canonical_url,
                  json.dumps(payload, ensure_ascii=False), max_attempts, now, now, now))
            row = conn.execute("SELECT * FROM clip_jobs WHERE job_id=?", (job_id,)).fetchone()
            return dict(row), True

    def claim_next_job(self, now: float | None = None) -> dict | None:
        now = now or time.time()
        with self._conn() as conn:
            row = conn.execute("""
                SELECT * FROM clip_jobs
                WHERE status IN ('pending', 'retry') AND next_run_at <= ?
                ORDER BY next_run_at ASC, created_at ASC LIMIT 1
            """, (now,)).fetchone()
            if not row:
                return None
            cur = conn.execute("""
                UPDATE clip_jobs SET status='running', attempts=attempts+1, updated_at=?
                WHERE job_id=? AND status IN ('pending', 'retry')
            """, (now, row["job_id"]))
            if cur.rowcount == 0:
                return None
            updated = conn.execute("SELECT * FROM clip_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
            return dict(updated)

    def complete_job(self, job_id: str, *, page_url: str = "", fetch_tier: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE clip_jobs SET status='done', page_url=?, fetch_tier=?, error='', updated_at=? WHERE job_id=?",
                (page_url, fetch_tier, time.time(), job_id),
            )

    def fail_job(self, job_id: str, error: str, *, retryable: bool = True) -> str:
        now = time.time()
        with self._conn() as conn:
            row = conn.execute("SELECT attempts, max_attempts FROM clip_jobs WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return "missing"
            if retryable and row["attempts"] < row["max_attempts"]:
                delay = min(60.0, float(2 ** row["attempts"]))
                conn.execute(
                    "UPDATE clip_jobs SET status='retry', next_run_at=?, error=?, updated_at=? WHERE job_id=?",
                    (now + delay, error[:500], now, job_id),
                )
                return "retry"
            conn.execute(
                "UPDATE clip_jobs SET status='dead', error=?, updated_at=? WHERE job_id=?",
                (error[:500], now, job_id),
            )
            return "dead"

    def get_job(self, job_id: str, tenant_id: int | None = None) -> dict | None:
        with self._conn() as conn:
            if tenant_id is None:
                row = conn.execute("SELECT * FROM clip_jobs WHERE job_id=?", (job_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM clip_jobs WHERE job_id=? AND tenant_id=?", (job_id, tenant_id)).fetchone()
            return dict(row) if row else None

    # ---------- Video Cache ----------
    def get_video_cache(self, cache_key: str, max_age_sec: int = 7 * 86400) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT payload_json, updated_at FROM video_cache WHERE cache_key=?", (cache_key,)).fetchone()
            if not row or time.time() - row["updated_at"] > max_age_sec:
                return None
            return json.loads(row["payload_json"])

    def set_video_cache(self, cache_key: str, payload: dict) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO video_cache (cache_key, payload_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at
            """, (cache_key, json.dumps(payload, ensure_ascii=False), time.time()))


_stores: dict[str, ClipStore] = {}
_store_lock = threading.Lock()


def store_available() -> bool:
    if env("CLIPPER_DB_PATH"):
        return True
    if "unittest" in sys.modules:
        return False
    return True


def get_store(db_path: Path | str | None = None) -> ClipStore:
    resolved = str((Path(db_path) if db_path is not None else default_db_path()).resolve())
    with _store_lock:
        if resolved not in _stores:
            _stores[resolved] = ClipStore(resolved)
        return _stores[resolved]
