from __future__ import annotations

import asyncio
import unittest

from astrbot_plugin_alice_image_assistant.alice_image.reverse.models import (
    SearchResultItem,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.service import (
    AliceImageReverseService,
)


class _Strategy:
    """最小策略桩：产出可控数量的结果."""

    def __init__(self, name: str, count: int, score_base: float = 0.0) -> None:
        self.name = name
        self.count = count
        self.score_base = score_base

    def get_service_name(self) -> str:
        return self.name

    async def search(self, _image_url: str):
        return [
            SearchResultItem(
                title=f"{self.name}-{index}",
                url=f"https://example.com/{self.name}/{index}",
                source=self.name,
                score=self.score_base - index * 0.01 if self.score_base else None,
            )
            for index in range(self.count)
        ]


class _SlowStrategy(_Strategy):
    """永远不返回的策略，用于验证单策略超时不牵连其它策略."""

    async def search(self, _image_url: str):
        await asyncio.sleep(60)
        return []


class _BrokenStrategy(_Strategy):
    """直接抛异常的策略."""

    async def search(self, _image_url: str):
        raise RuntimeError("boom")


class _ThumbStrategy(_Strategy):
    """带缩略图的策略，fetch_thumbnail 可控制成功/失败."""

    def __init__(self, name: str, count: int, fail: bool) -> None:
        super().__init__(name, count)
        self.fail = fail

    async def search(self, _image_url: str):
        return [
            SearchResultItem(
                title=f"{self.name}-{index}",
                url=f"https://example.com/{self.name}/{index}",
                thumbnail=f"https://cdn.example.com/{self.name}/{index}.jpg",
                source=self.name,
                score=0.5,
            )
            for index in range(self.count)
        ]

    async def fetch_thumbnail(self, url: str) -> bytes | None:
        if self.fail:
            raise RuntimeError("thumbnail down")
        return b"png-bytes"


class ReverseResultLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_results_is_the_final_display_limit(self) -> None:
        """max_results 是最终展示上限，不是每引擎上限."""
        service = AliceImageReverseService(
            [_Strategy("one", 5), _Strategy("two", 5)],
            max_results=2,
        )
        result = await service.explore("https://example.com/input.jpg")

        self.assertEqual(len(result.items), 2)
        # 每引擎抓取上限可以大于展示上限，否则跨引擎去重后凑不满展示条数
        self.assertGreaterEqual(service.per_engine_fetch, service.max_results)
        self.assertEqual(service.per_engine_fetch, 5)

    async def test_string_config_values_do_not_crash(self) -> None:
        """WebUI 传字符串数值时不应抛 TypeError."""
        service = AliceImageReverseService(
            [_Strategy("one", 3)],
            max_results="2",  # type: ignore[arg-type]
            total_timeout_seconds="30",  # type: ignore[arg-type]
        )
        self.assertEqual(service.max_results, 2)
        self.assertEqual(service.total_timeout_seconds, 30)

    async def test_scored_results_outrank_unscored_ones(self) -> None:
        """有置信度的结果必须排在无置信度结果之前."""
        service = AliceImageReverseService(
            [_Strategy("nolens", 3), _Strategy("sauce", 3, score_base=0.95)],
            max_results=4,
        )
        result = await service.explore("https://example.com/input.jpg")

        self.assertEqual([item.source for item in result.items][:3], ["sauce"] * 3)


class ReverseRobustnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_strategy_is_timed_out_without_losing_others(self) -> None:
        """慢策略超时后其它策略的结果必须保留."""
        service = AliceImageReverseService(
            [_SlowStrategy("slow", 3), _Strategy("fast", 3, score_base=0.9)],
            max_results=5,
            total_timeout_seconds=10,
        )
        # 直接改实例属性绕过构造函数的下限夹取：用极短超时让慢策略立刻被砍掉，
        # 同时留出足够时间让快策略正常返回，以此证明超时是"按策略"而非"一刀切"
        service.total_timeout_seconds = 0.05

        result = await service.explore("https://example.com/input.jpg")

        self.assertEqual(len(result.items), 3)
        self.assertTrue(all(item.source == "fast" for item in result.items))

    async def test_broken_strategy_does_not_kill_the_batch(self) -> None:
        service = AliceImageReverseService(
            [_BrokenStrategy("bad", 3), _Strategy("good", 2, score_base=0.8)],
            max_results=5,
        )
        result = await service.explore("https://example.com/input.jpg")

        self.assertEqual([item.source for item in result.items], ["good", "good"])

    async def test_thumbnail_failure_degrades_to_no_image(self) -> None:
        """缩略图失败只丢图，不能丢结果."""
        service = AliceImageReverseService([_ThumbStrategy("t", 3, fail=True)])
        result = await service.explore("https://example.com/input.jpg")

        self.assertEqual(len(result.items), 3)
        self.assertTrue(all(item.thumbnail_bytes is None for item in result.items))

    async def test_strategy_thumbnail_hook_is_used(self) -> None:
        """策略自带的 fetch_thumbnail 钩子必须被 service 调用."""
        service = AliceImageReverseService([_ThumbStrategy("t", 2, fail=False)])
        result = await service.explore("https://example.com/input.jpg")

        self.assertEqual(len(result.items), 2)
        self.assertTrue(all(item.thumbnail_bytes == b"png-bytes" for item in result.items))


if __name__ == "__main__":
    unittest.main()
