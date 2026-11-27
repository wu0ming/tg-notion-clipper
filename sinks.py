"""Pluggable sink abstraction (#13) decouples archiving logic from specific storage backends."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import notion_writer


class BaseSink(ABC):
    @abstractmethod
    async def exists(self, canonical_url: str, *, project_key: str = "", clip_key: str = "") -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def create_item(self, payload: dict[str, Any]) -> tuple[str, bool]:
        raise NotImplementedError

    @abstractmethod
    async def append_note(self, item_id: str, text: str, *, chat_id: int = 0, target_id: int = 0,
                          message_id: int = 0) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def update_tags(self, item_id: str, tags: list[str]) -> list[str]:
        raise NotImplementedError


class NotionSink(BaseSink):
    def __init__(self, scope: dict | None = None) -> None:
        self.scope = scope

    async def exists(self, canonical_url: str, *, project_key: str = "", clip_key: str = "") -> str | None:
        if existing := await notion_writer.find_existing(
            canonical_url, project_key=project_key, clip_key=clip_key, scope=self.scope
        ):
            return existing
        has_inbox = bool(self.scope.get("inbox_db")) if self.scope is not None else bool(notion_writer.env("NOTION_DB_INBOX"))
        if has_inbox:
            return await notion_writer.find_existing(
                canonical_url, inbox=True, project_key=project_key, clip_key=clip_key, scope=self.scope
            )
        return None

    async def create_item(self, payload: dict[str, Any]) -> tuple[str, bool]:
        kwargs = {k: v for k, v in payload.items() if k not in {
            "result", "source_type", "url", "status", "raw_content", "user_note", "fetch_note"
        }}
        kwargs["scope"] = self.scope
        return await notion_writer.save_to_notion(
            payload["result"],
            payload["source_type"],
            payload.get("url", ""),
            payload["status"],
            payload.get("raw_content", ""),
            payload.get("user_note", ""),
            payload.get("fetch_note", ""),
            **kwargs,
        )

    async def append_note(self, item_id: str, text: str, *, chat_id: int = 0, target_id: int = 0,
                          message_id: int = 0) -> str | None:
        if chat_id and target_id:
            return await notion_writer.append_telegram_note(chat_id, target_id, message_id, text, scope=self.scope)
        url, _ = await notion_writer.enrich_notion_page(
            item_id, source_type="Note", user_note=text, source_key=f"note-{message_id or hash(text)}",
            scope=self.scope,
        )
        return url

    async def update_tags(self, item_id: str, tags: list[str]) -> list[str]:
        return await notion_writer.update_page_tags(item_id, tags, scope=self.scope)

    async def enrich_item(self, item_id: str, **kwargs: Any) -> tuple[str, bool]:
        return await notion_writer.enrich_notion_page(item_id, scope=self.scope, **kwargs)
