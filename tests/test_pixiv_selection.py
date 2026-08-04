from __future__ import annotations

import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_alice_image_assistant.alice_image.forward.pixiv_search import (
    PixivForwardSearchService,
)
from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.selection import (
    PixivSelectionPolicy,
)


class PixivSelectionPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.history: defaultdict[str, dict[int, int]] = defaultdict(dict)
        self.clock = 0

        def get_history(scope_id: str, _days: int) -> dict[int, int]:
            return dict(self.history[scope_id])

        def add_ids(illust_ids, scope_id: str) -> None:
            for value in illust_ids:
                self.clock += 1
                self.history[scope_id][int(value)] = self.clock

        self.patchers = [
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.selection.get_recent_sent_illust_history",
                side_effect=get_history,
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.selection.add_recent_sent_illusts",
                side_effect=add_ids,
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.selection.cleanup_old_recent_sent_illusts"
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()

    @staticmethod
    def _policy(**overrides) -> PixivSelectionPolicy:
        values = {
            "randomize_search_results": False,
            "recent_dedup_enabled": True,
            "recent_dedup_retention_days": 7,
        }
        values.update(overrides)
        config = SimpleNamespace(**values)
        return PixivSelectionPolicy(config)

    async def test_same_conversation_uses_next_unseen_work(self) -> None:
        policy = self._policy()
        event = SimpleNamespace(unified_msg_origin="test:group:100")
        works = [SimpleNamespace(id=value) for value in (1, 2, 3)]

        first = await policy.select(event, works, 1)
        second = await policy.select(event, works, 1)

        self.assertEqual([item.id for item in first], [1])
        self.assertEqual([item.id for item in second], [2])

    async def test_history_is_isolated_by_unified_message_origin(self) -> None:
        policy = self._policy()
        works = [SimpleNamespace(id=1), SimpleNamespace(id=2)]

        first = await policy.select(
            SimpleNamespace(unified_msg_origin="test:group:100"), works, 1
        )
        other_group = await policy.select(
            SimpleNamespace(unified_msg_origin="test:group:200"), works, 1
        )

        self.assertEqual(first[0].id, 1)
        self.assertEqual(other_group[0].id, 1)

    async def test_exhausted_pool_allows_old_work_as_fallback(self) -> None:
        policy = self._policy()
        event = SimpleNamespace(unified_msg_origin="test:private:user")
        works = [SimpleNamespace(id=1), SimpleNamespace(id=2)]
        self.history[event.unified_msg_origin].update(
            {
                1: 1,
                2: 2,
            }
        )

        selected = await policy.select(event, works, 1)

        self.assertEqual(selected[0].id, 1)

    async def test_preview_selection_does_not_consume_history(self) -> None:
        policy = self._policy()
        event = SimpleNamespace(unified_msg_origin="test:group:preview")
        works = [SimpleNamespace(id=1), SimpleNamespace(id=2)]

        selected = await policy.select(event, works, 2, remember=False)

        self.assertEqual([item.id for item in selected], [1, 2])
        self.assertEqual(self.history[event.unified_msg_origin], {})

    async def test_partial_fresh_pool_is_filled_with_oldest_history(self) -> None:
        policy = self._policy()
        event = SimpleNamespace(unified_msg_origin="test:group:partial")
        works = [SimpleNamespace(id=value) for value in (1, 2, 3)]
        self.history[event.unified_msg_origin].update({1: 1, 2: 2})

        selected = await policy.select(event, works, 3)

        self.assertEqual([item.id for item in selected], [3, 1, 2])

    async def test_concurrent_selection_in_same_scope_does_not_overlap(self) -> None:
        policy = self._policy()
        event = SimpleNamespace(unified_msg_origin="test:group:concurrent")
        works = [SimpleNamespace(id=value) for value in (1, 2, 3)]

        first, second = await asyncio.gather(
            policy.select(event, works, 1),
            policy.select(event, works, 1),
        )

        self.assertNotEqual(first[0].id, second[0].id)

    async def test_randomization_can_be_disabled_per_selection(self) -> None:
        policy = self._policy(randomize_search_results=True)
        policy.config.recent_dedup_enabled = False
        event = SimpleNamespace(unified_msg_origin="test:group:random")
        works = [SimpleNamespace(id=1), SimpleNamespace(id=2)]

        selected = await policy.select(event, works, 1, randomize=False)

        self.assertEqual(selected[0].id, 1)


class PixivForwardDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_results_are_removed_before_visual_review(self) -> None:
        works = [SimpleNamespace(id=value) for value in (1, 2, 3)]

        class _Policy:
            def __init__(self) -> None:
                self.calls = []

            async def select(self, _event, items, count, **kwargs):
                self.calls.append((list(items), count, kwargs))
                if kwargs.get("fill_from_history") is False:
                    return list(items)[1:]
                return list(items)[:count]

        policy = _Policy()
        service = PixivForwardSearchService.__new__(PixivForwardSearchService)
        service.controller = SimpleNamespace(
            client_wrapper=SimpleNamespace(authenticate=AsyncMock(return_value=True)),
            features={},
            selection_policy=policy,
        )
        service._collect = AsyncMock(return_value=(works, [], ""))
        service._filter = Mock(return_value=works)
        service._review = AsyncMock(return_value=([works[1]], False, ""))
        event = SimpleNamespace(unified_msg_origin="test:group:llm")

        result = await service.search(
            event,
            "角色",
            "精确描述",
            count=1,
            send_images=False,
        )

        reviewed_items = service._review.await_args.args[1]
        self.assertEqual([item.id for item in reviewed_items], [2, 3])
        self.assertEqual(result.ids, [2])
        self.assertFalse(policy.calls[-1][2]["remember"])


if __name__ == "__main__":
    unittest.main()
