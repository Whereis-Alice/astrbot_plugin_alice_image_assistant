"""Short-lived single-flight cache scoped by conversation and user message."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .models import ExplorationResult, SearchResultItem


@dataclass
class ToolEvidence:
    result: ExplorationResult
    items: list[SearchResultItem]
    visual: dict[str, Any]
    sent: bool = False
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ToolEvidenceCache:
    """At most 16 pending/completed searches; no sharing across user turns."""

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple, tuple[float, asyncio.Task]] = OrderedDict()

    async def get(
        self,
        key: tuple,
        factory: Callable[[], Awaitable[ToolEvidence]],
    ) -> tuple[ToolEvidence, bool]:
        now = time.monotonic()
        for old_key, (created, task) in list(self._entries.items()):
            if task.done() and (
                now - created > 180 or task.cancelled() or task.exception() is not None
            ):
                self._entries.pop(old_key)
        existing = self._entries.get(key)
        if existing:
            self._entries.move_to_end(key)
            return await asyncio.shield(existing[1]), True
        if len(self._entries) >= 16:
            completed = next((k for k, (_, t) in self._entries.items() if t.done()), None)
            if completed is None:
                return await factory(), False
            self._entries.pop(completed)
        task = asyncio.create_task(factory())
        # Retrieve background failures even if the original caller was cancelled.
        task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        self._entries[key] = (now, task)
        return await asyncio.shield(task), False

    async def close(self) -> None:
        tasks = [task for _, task in self._entries.values()]
        self._entries.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
