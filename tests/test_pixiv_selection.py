from __future__ import annotations

import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_alice_image_assistant.alice_image.forward.final_verify import (
    FinalVerdict,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.pixiv_search import (
    PixivForwardSearchService,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.review import ReviewStatus
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

    async def test_strict_review_no_match_does_not_reinsert_pixiv_candidates(
        self,
    ) -> None:
        works = [SimpleNamespace(id=value) for value in (1, 2)]
        service = PixivForwardSearchService.__new__(PixivForwardSearchService)
        service.controller = SimpleNamespace(
            client_wrapper=SimpleNamespace(authenticate=AsyncMock(return_value=True)),
            features={},
            selection_policy=None,
        )
        service._collect = AsyncMock(return_value=(works, [], ""))
        service._filter = Mock(return_value=works)
        service._review = AsyncMock(
            return_value=(
                [],
                ReviewStatus.NO_MATCH,
                "视觉审核没有选出符合描述的 Pixiv 作品。",
            )
        )

        result = await service.search(
            SimpleNamespace(unified_msg_origin="test:group:pixiv-no-match"),
            "海狸",
            "野生海狸真实照片",
            count=1,
            send_images=False,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.ids, [])
        self.assertIn("没有选出", result.error)

    async def test_strict_review_does_not_fill_unreviewed_pixiv_candidates(
        self,
    ) -> None:
        works = [SimpleNamespace(id=value) for value in (1, 2, 3)]
        service = PixivForwardSearchService.__new__(PixivForwardSearchService)
        service.review_config = {"strict_match_enabled": True}
        service.controller = SimpleNamespace(
            _get_http_session=AsyncMock(return_value=object())
        )
        service._provider = AsyncMock(return_value=object())
        service._preview_url = lambda item: f"https://example.com/{item.id}.jpg"
        service._collage = SimpleNamespace(
            create_collage_from_items=AsyncMock(
                return_value=(
                    b"collage",
                    [
                        (f"https://example.com/{item.id}.jpg", b"image")
                        for item in works
                    ],
                )
            )
        )
        service._review_lock = asyncio.Semaphore(1)

        with (
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.pixiv_search.download_image",
                AsyncMock(return_value=b"image"),
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.pixiv_search.select_from_collage",
                AsyncMock(return_value=[2]),
            ),
        ):
            selected, status, error = await service._review(
                SimpleNamespace(unified_msg_origin="test"),
                works,
                "海狸真实照片",
                count=3,
            )

        self.assertEqual([item.id for item in selected], [2])
        self.assertEqual(status, ReviewStatus.MATCHED)
        self.assertEqual(error, "")


class PixivFinalVerifyTests(unittest.IsolatedAsyncioTestCase):
    """Pixiv 精确找图的终选全分辨率复核。

    拼图每格只有 300x300，细节几乎全丢；这几条用例锁住"复核拒绝就不要发"、
    "复核自身报错不许丢图"、"开关关掉就完全不调用模型"三条语义。
    """

    @staticmethod
    def _service(**review_overrides) -> PixivForwardSearchService:
        works = [SimpleNamespace(id=value) for value in (1, 2, 3)]
        service = PixivForwardSearchService.__new__(PixivForwardSearchService)
        service.review_config = {"strict_match_enabled": True, **review_overrides}
        service.controller = SimpleNamespace(
            _get_http_session=AsyncMock(return_value=object())
        )
        service._provider = AsyncMock(return_value=object())
        service._preview_url = lambda item: f"https://example.com/{item.id}.jpg"
        service._collage = SimpleNamespace(
            create_collage_from_items=AsyncMock(
                return_value=(
                    b"collage",
                    [
                        (f"https://example.com/{item.id}.jpg", b"image-bytes")
                        for item in works
                    ],
                )
            )
        )
        service._review_lock = asyncio.Semaphore(1)
        return service, works

    async def _run_review(self, service, works, verify_mock, count: int = 1):
        with (
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.pixiv_search.download_image",
                AsyncMock(return_value=b"image-bytes"),
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.pixiv_search.select_from_collage",
                AsyncMock(return_value=[2]),
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.pixiv_search.verify_candidate",
                verify_mock,
            ),
        ):
            return await service._review(
                SimpleNamespace(unified_msg_origin="test"),
                works,
                "银发红瞳少女，黑色哥特长裙",
                count=count,
            )

    async def test_rejected_candidate_is_not_sent_in_strict_mode(self) -> None:
        service, works = self._service(final_verify_retry_limit=0)
        verify = AsyncMock(
            return_value=FinalVerdict(match=False, confidence=0.1, reason="发色不符")
        )

        selected, status, error = await self._run_review(service, works, verify)

        self.assertEqual(selected, [])
        self.assertEqual(status, ReviewStatus.NO_MATCH)
        self.assertIn("发色不符", error)

    async def test_rejected_candidate_falls_back_when_strict_disabled(self) -> None:
        service, works = self._service(
            strict_match_enabled=False, final_verify_retry_limit=0
        )
        verify = AsyncMock(
            return_value=FinalVerdict(match=False, confidence=0.1, reason="发色不符")
        )

        selected, status, _error = await self._run_review(service, works, verify)

        self.assertEqual([item.id for item in selected], [1])
        self.assertEqual(status, ReviewStatus.NO_MATCH)

    async def test_low_confidence_is_rejected_even_when_match_is_true(self) -> None:
        service, works = self._service(
            confidence_threshold=0.8, final_verify_retry_limit=0
        )
        verify = AsyncMock(
            return_value=FinalVerdict(match=True, confidence=0.5, reason="不太确定")
        )

        selected, status, _error = await self._run_review(service, works, verify)

        self.assertEqual(selected, [])
        self.assertEqual(status, ReviewStatus.NO_MATCH)

    async def test_verify_error_keeps_collage_choice(self) -> None:
        service, works = self._service()
        verify = AsyncMock(return_value=FinalVerdict(error="provider 调用失败"))

        selected, status, error = await self._run_review(service, works, verify)

        self.assertEqual([item.id for item in selected], [2])
        self.assertEqual(status, ReviewStatus.MATCHED)
        self.assertEqual(error, "")

    async def test_accepted_candidate_passes_through(self) -> None:
        service, works = self._service()
        verify = AsyncMock(
            return_value=FinalVerdict(match=True, confidence=0.9, reason="逐条命中")
        )

        selected, status, _error = await self._run_review(service, works, verify)

        self.assertEqual([item.id for item in selected], [2])
        self.assertEqual(status, ReviewStatus.MATCHED)
        self.assertEqual(verify.await_count, 1)

    async def test_disabled_switch_skips_the_model_entirely(self) -> None:
        service, works = self._service(final_verify_enabled=False)
        verify = AsyncMock(return_value=FinalVerdict(match=False))

        selected, status, _error = await self._run_review(service, works, verify)

        self.assertEqual([item.id for item in selected], [2])
        self.assertEqual(status, ReviewStatus.MATCHED)
        verify.assert_not_awaited()

    async def test_verify_uses_bounded_max_edge(self) -> None:
        service, works = self._service(final_verify_max_edge=99999)
        verify = AsyncMock(return_value=FinalVerdict(match=True, confidence=0.9))

        await self._run_review(service, works, verify)

        self.assertEqual(verify.await_args.kwargs["max_edge"], 2048)

    def test_configure_review_refreshes_config_and_rejects_non_dict(self) -> None:
        """编排层每次搜索都会重新下发配置，pixiv 线必须能热更新且容忍脏值。"""
        service, _works = self._service()

        service.configure_review({"confidence_threshold": 0.9})
        self.assertEqual(service._confidence_threshold(), 0.9)

        service.configure_review(None)
        self.assertEqual(service.review_config, {})
        self.assertTrue(service._final_verify_enabled())


if __name__ == "__main__":
    unittest.main()
