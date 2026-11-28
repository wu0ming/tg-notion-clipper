"""Telegram album (media_group_id) aggregation and knowledge-unit splitting (#5)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import re
import time
from typing import Awaitable, Callable

from config import env
from telegram_media import extract_media
from telegram_source import explicit_note, forward_source
from utils import URL_RE, clean_tags, detect_source, extract_url

log = logging.getLogger("clipper")

MERGE_DIRECTIVE_RE = re.compile(r"#合并\b|合并成一篇|全部合并|合为一篇|放在一起", re.IGNORECASE)
SPLIT_DIRECTIVE_RE = re.compile(r"#拆分\b|#独立\b|单独保存|逐条保存|拆开保存|分开保存|每个.*单独", re.IGNORECASE)
CONTROL_TAG_RE = re.compile(r"(?<!\w)#(?:合并|拆分|独立)\b")


@dataclass
class AlbumMember:
    message_id: int
    message: dict
    text: str
    entities: list
    forward: dict
    raw_url: str | None
    accompanying: str
    explicit_user_note: str | None
    media: object | None


@dataclass
class KnowledgeUnit:
    unit_index: int
    members: list[AlbumMember]
    topic: str = ""
    reason: str = ""
    mode: str = "merge"


def parse_album_member(message: dict) -> AlbumMember:
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    forward = forward_source(message)
    raw_url, accompanying = extract_url(text, entities)
    explicit = None if forward else explicit_note(accompanying if raw_url else text.strip())
    return AlbumMember(
        message_id=int(message.get("message_id") or 0),
        message=message,
        text=text,
        entities=entities,
        forward=forward,
        raw_url=raw_url,
        accompanying=accompanying,
        explicit_user_note=explicit,
        media=extract_media(message),
    )


def strip_control_hashtags(text: str) -> str:
    return re.sub(r"[ \t]+", " ", CONTROL_TAG_RE.sub(" ", text or "")).strip()


def _validate_groups(groups: list[dict], expected_ids: list[int]) -> list[list[int]] | None:
    seen: list[int] = []
    out: list[list[int]] = []
    expected_set = set(expected_ids)
    for g in groups:
        mids = g.get("member_ids")
        if not isinstance(mids, list) or not mids:
            return None
        parsed = []
        for mid in mids:
            try:
                val = int(mid)
            except (TypeError, ValueError):
                return None
            if val not in expected_set or val in seen:
                return None
            seen.append(val)
            parsed.append(val)
        out.append(parsed)
    if set(seen) != expected_set:
        return None
    return out


async def plan_knowledge_units(
    members: list[AlbumMember],
    downloaded_media: dict[int, tuple[bytes, str, str]],
    llm_planner: Callable[[list[dict], list[tuple[int, bytes, str]]], Awaitable[dict | None]] | None = None,
) -> list[KnowledgeUnit]:
    """先确定性规则（0 Token），仅在知识单元存在歧义时调用一次轻量多模态决策。"""
    ordered = sorted(members, key=lambda m: m.message_id)
    if len(ordered) <= 1:
        return [KnowledgeUnit(unit_index=0, members=ordered, mode="merge", reason="single_member")]

    combined_user_text = "\n".join(m.text for m in ordered if m.text and not m.forward)
    all_text = "\n".join(m.text for m in ordered if m.text)
    if SPLIT_DIRECTIVE_RE.search(combined_user_text):
        return [KnowledgeUnit(unit_index=i, members=[m], mode="split", reason="explicit_split")
                for i, m in enumerate(ordered)]
    if MERGE_DIRECTIVE_RE.search(combined_user_text):
        return [KnowledgeUnit(unit_index=0, members=ordered, mode="merge", reason="explicit_merge")]

    # 强关联信号（0 Token）：仅一条有效正文且具备完整项目/文章描述或非视频唯一项目链接（如 MuseLink / FLYX）
    non_empty_texts = [m for m in ordered if m.text.strip()]
    unique_urls = list(dict.fromkeys(m.raw_url for m in ordered if m.raw_url))
    if len(non_empty_texts) == 1:
        sole = non_empty_texts[0]
        non_url_text = (sole.accompanying if sole.raw_url else sole.text).strip()
        is_video_url = len(unique_urls) == 1 and detect_source(unique_urls[0]) in {"YouTube", "Bilibili"}
        if len(non_url_text) >= 25 or (len(unique_urls) == 1 and not is_video_url and len(non_url_text) >= 10):
            return [KnowledgeUnit(unit_index=0, members=ordered, mode="merge", reason="single_project_or_intro")]
    if not non_empty_texts and all(m.media and m.media.input_type != "Image" for m in ordered):
        return [KnowledgeUnit(unit_index=0, members=ordered, mode="merge", reason="homogeneous_non_image")]

    # 存在歧义（多张无说明图片、短通用说明、或多条不同配文/链接）：按需调用一次轻量多模态分组器
    if llm_planner is not None:
        summary = [{
            "member_id": m.message_id,
            "input_type": m.media.input_type if m.media else "Text",
            "url": m.raw_url or "",
            "text": strip_control_hashtags(m.text)[:300],
        } for m in ordered]
        parts = [(m.message_id, downloaded_media[m.message_id][0], downloaded_media[m.message_id][1])
                 for m in ordered if m.message_id in downloaded_media and downloaded_media[m.message_id][1].startswith("image/")]
        decision = await llm_planner(summary, parts)
        if decision and float(decision.get("confidence", 0.7)) >= 0.45:
            validated = _validate_groups(decision.get("groups") or [], [m.message_id for m in ordered])
            if validated:
                by_id = {m.message_id: m for m in ordered}
                mode = str(decision.get("mode") or "mixed")
                units = []
                for idx, (group_ids, raw_g) in enumerate(zip(validated, decision["groups"])):
                    units.append(KnowledgeUnit(
                        unit_index=idx,
                        members=[by_id[mid] for mid in group_ids],
                        topic=str(raw_g.get("topic") or ""),
                        reason=str(raw_g.get("reason") or mode),
                        mode=mode,
                    ))
                return units

    return [KnowledgeUnit(unit_index=0, members=ordered, mode="merge", reason="safe_default_merge")]


def combine_unit_materials(unit: KnowledgeUnit) -> dict:
    """合并同一知识单元内的文本、转发来源、批注和标签，保留成员归属但不重复拷贝相同说明。"""
    raw_texts: list[str] = []
    forwarded_texts: list[str] = []
    user_notes: list[str] = []
    accompanying_texts: list[str] = []
    user_tags: list[str] = []
    forward_meta: dict = {}
    urls: list[str] = []

    def append_unique(target: list[str], value: str) -> None:
        val = (value or "").strip()
        if val and val not in target:
            target.append(val)

    for m in unit.members:
        cleaned = strip_control_hashtags(m.text)
        if m.raw_url and m.raw_url not in urls:
            urls.append(m.raw_url)
        if m.forward:
            append_unique(forwarded_texts, cleaned)
            if not forward_meta:
                forward_meta = dict(m.forward)
        elif cleaned:
            append_unique(raw_texts, cleaned)
            note_src = (
                strip_control_hashtags(m.explicit_user_note)
                if m.explicit_user_note is not None
                else strip_control_hashtags(m.accompanying if m.raw_url else cleaned)
            )
            if note_src:
                append_unique(user_notes, note_src)
            tag_src = strip_control_hashtags(m.explicit_user_note if m.explicit_user_note is not None else cleaned)
            for tag in clean_tags(re.findall(r"(?<!\w)#([\w]+)", URL_RE.sub(" ", tag_src)), limit=8):
                if tag not in user_tags:
                    user_tags.append(tag)

    return {
        "raw_url": urls[0] if urls else None,
        "all_urls": urls,
        "user_note": "\n".join(user_notes),
        "accompanying_text": "",
        "forwarded_text": "\n\n".join(forwarded_texts),
        "forward_metadata": forward_meta,
        "plain_text": "\n\n".join(raw_texts),
        "user_tags": user_tags[:5],
    }


@dataclass
class _AlbumSession:
    album_key: str
    user_id: int
    chat_id: int
    members: dict[int, dict] = field(default_factory=dict)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    done_future: asyncio.Future | None = None
    started_at: float = field(default_factory=time.monotonic)
    last_added_at: float = field(default_factory=time.monotonic)


class AlbumCoordinator:
    """按 (loop, user_id, chat_id, media_group_id) 在静默窗口内聚合同一相册的并发/顺序消息。"""

    def __init__(self) -> None:
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}
        self._sessions: dict[tuple[asyncio.AbstractEventLoop, str], _AlbumSession] = {}
        self._completed: dict[str, float] = {}

    def _lock_for(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        return self._locks.setdefault(loop, asyncio.Lock())

    @staticmethod
    def album_key(user_id: int, chat_id: int, media_group_id: str) -> str:
        return f"album:{user_id}:{chat_id}:{media_group_id}"

    async def submit(
        self,
        *,
        user_id: int,
        chat_id: int,
        media_group_id: str,
        message: dict,
        on_ready: Callable[[list[dict]], Awaitable[tuple[str, str]]],
        on_late: Callable[[str, dict], Awaitable[tuple[str, str] | None]] | None = None,
        on_member_arrived: Callable[[dict], Awaitable[None]] | None = None,
    ) -> tuple[str, str, bool] | None:
        """返回 (receipt, reaction, is_leader) 或 None（表示跟随者，由 leader 统一发送回执）。"""
        loop = asyncio.get_running_loop()
        key = self.album_key(user_id, chat_id, media_group_id)
        msg_id = int(message.get("message_id") or 0)
        session_key = (loop, key)

        if on_member_arrived is not None:
            await on_member_arrived(message)

        try:
            from clip_store import get_store
            store = get_store()
            store.record_album_member(key, user_id, msg_id, message)
        except Exception:
            store = None

        async with self._lock_for(loop):
            existing = self._sessions.get(session_key)
            if existing is not None:
                existing.members[msg_id] = message
                existing.last_added_at = time.monotonic()
                existing.event.set()
                fut = existing.done_future
                leader = False
            elif key in self._completed or (store and store.get_album_units(key, user_id)):
                if on_late is not None:
                    res = await on_late(key, message)
                    if res is not None:
                        return res[0], res[1], True
                return None
            else:
                fut = loop.create_future()
                existing = _AlbumSession(album_key=key, user_id=user_id, chat_id=chat_id, done_future=fut)
                existing.members[msg_id] = message
                self._sessions[session_key] = existing
                leader = True

        if not leader:
            if fut is not None:
                await fut
            return None

        silence = float(env("ALBUM_SILENCE_SECONDS") or 2.0)
        max_wait = float(env("ALBUM_MAX_WAIT_SECONDS") or 8.0)
        try:
            while True:
                existing.event.clear()
                elapsed = time.monotonic() - existing.started_at
                remaining_max = max_wait - elapsed
                if remaining_max <= 0:
                    break
                wait_for = min(silence, remaining_max)
                try:
                    await asyncio.wait_for(existing.event.wait(), timeout=wait_for)
                except asyncio.TimeoutError:
                    break

            async with self._lock_for(loop):
                self._sessions.pop(session_key, None)
                self._completed[key] = time.monotonic()
                members = [existing.members[k] for k in sorted(existing.members)]

            receipt, reaction = await on_ready(members)
            return receipt, reaction, True
        finally:
            async with self._lock_for(loop):
                self._sessions.pop(session_key, None)
            if fut and not fut.done():
                fut.set_result(True)


coordinator = AlbumCoordinator()
