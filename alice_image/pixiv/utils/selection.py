"""Session-scoped Pixiv result randomization and recent-result deduplication."""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from astrbot.api import logger

from .database import (
    add_recent_sent_illusts,
    cleanup_old_recent_sent_illusts,
    get_recent_sent_illust_history,
)

SelectionCallback = Callable[[list[Any], int], Awaitable[list[Any]]]


class PixivSelectionPolicy:
    """Select varied Pixiv results without repeating them within one conversation."""

    _CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60

    def __init__(self, pixiv_config: Any) -> None:
        self.config = pixiv_config
        self._scope_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._cleanup_lock = asyncio.Lock()
        self._last_cleanup_at = float("-inf")

    @staticmethod
    def _scope_id(event: Any) -> str:
        for attr in ("unified_msg_origin", "session_id"):
            value = str(getattr(event, attr, "") or "").strip()
            if value:
                return value
        return f"event:{id(event)}"

    @staticmethod
    def _item_id(item: Any) -> int | None:
        raw_id = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _unique_items(cls, items: Sequence[Any]) -> list[Any]:
        unique: list[Any] = []
        seen_ids: set[int] = set()
        seen_objects: set[int] = set()
        for item in items:
            item_id = cls._item_id(item)
            if item_id is not None:
                if item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
            else:
                object_id = id(item)
                if object_id in seen_objects:
                    continue
                seen_objects.add(object_id)
            unique.append(item)
        return unique

    async def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup_at < self._CLEANUP_INTERVAL_SECONDS:
            return
        async with self._cleanup_lock:
            now = time.monotonic()
            if now - self._last_cleanup_at < self._CLEANUP_INTERVAL_SECONDS:
                return
            await asyncio.to_thread(
                cleanup_old_recent_sent_illusts,
                self.config.recent_dedup_retention_days,
            )
            self._last_cleanup_at = now

    async def select(
        self,
        event: Any,
        items: Sequence[Any],
        count: int,
        *,
        randomize: bool | None = None,
        remember: bool = True,
        fill_from_history: bool = True,
    ) -> list[Any]:
        """Select and optionally remember results as soon as sending starts."""

        candidates = self._unique_items(items)
        if not candidates or count <= 0:
            return []

        count = min(int(count), len(candidates))
        should_randomize = (
            self.config.randomize_search_results
            if randomize is None
            else bool(randomize)
        )
        dedup_enabled = bool(self.config.recent_dedup_enabled)
        scope_id = self._scope_id(event)

        if not dedup_enabled:
            if should_randomize:
                return random.sample(candidates, count)
            return candidates[:count]

        await self._maybe_cleanup()
        async with self._scope_locks[scope_id]:
            recent_history = await asyncio.to_thread(
                get_recent_sent_illust_history,
                scope_id,
                self.config.recent_dedup_retention_days,
            )
            fresh = [
                item
                for item in candidates
                if (item_id := self._item_id(item)) is None
                or item_id not in recent_history
            ]
            fresh_count = min(count, len(fresh))
            selected = (
                random.sample(fresh, fresh_count)
                if should_randomize
                else fresh[:fresh_count]
            )

            should_use_history = not fresh or (
                fill_from_history and len(selected) < count
            )
            if should_use_history:
                recent_candidates = [
                    item
                    for item in candidates
                    if (item_id := self._item_id(item)) is not None
                    and item_id in recent_history
                ]
                recent_candidates.sort(
                    key=lambda item: recent_history[self._item_id(item)]
                )
                needed = count - len(selected)
                selected.extend(recent_candidates[:needed])

            if not fresh:
                logger.info(
                    "[AliceImagePixiv] 会话 %s 的候选均在近期记录中，优先复用最久未发送的作品。",
                    scope_id,
                )
            if remember:
                selected_ids = [
                    item_id
                    for item in selected
                    if (item_id := self._item_id(item)) is not None
                ]
                if selected_ids:
                    await asyncio.to_thread(
                        add_recent_sent_illusts,
                        selected_ids,
                        scope_id,
                    )
            return selected

    def callback(
        self,
        event: Any,
        *,
        randomize: bool | None = None,
        remember: bool = True,
        fill_from_history: bool = True,
    ) -> SelectionCallback:
        async def choose(items: list[Any], count: int) -> list[Any]:
            return await self.select(
                event,
                items,
                count,
                randomize=randomize,
                remember=remember,
                fill_from_history=fill_from_history,
            )

        return choose
