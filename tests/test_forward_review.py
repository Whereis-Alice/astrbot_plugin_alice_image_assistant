from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_plugin_alice_image_assistant.alice_image.forward.review import ReviewStatus
from astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service import (
    SerpApiForwardService,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.vlm import (
    VlmReviewError,
    select_from_collage,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.soutu.service import (
    SoutuSearchService,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.soutu.composer import (
    ComposerManager,
)


class _Context:
    def __init__(self, provider=object()) -> None:
        self.provider = provider

    def get_provider_by_id(self, _provider_id):
        return self.provider

    async def get_current_chat_provider_id(self, _umo):
        return "provider"


class SoutuReviewTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _service(**config) -> SoutuSearchService:
        values = {
            "batch_size": 2,
            "review_rounds": 3,
            "primary_source_enabled": True,
            "bing_fallback_enabled": False,
        }
        values.update(config)
        service = SoutuSearchService(_Context(), values)
        service._collect_url_pools = AsyncMock(
            return_value=(["1", "2", "3", "4", "5", "6"], [], "")
        )
        service._download_valid_batch = AsyncMock(
            side_effect=[
                [("1", b"one"), ("2", b"two")],
                [("3", b"three"), ("4", b"four")],
                [("5", b"five"), ("6", b"six")],
            ]
        )
        service._ensure_bing_pool = AsyncMock(return_value=True)
        service._format_image = AsyncMock(side_effect=lambda value: value)
        return service

    async def test_first_batch_rejected_second_batch_match_is_sent(self) -> None:
        service = self._service()
        service._vlm_selection = AsyncMock(
            side_effect=[
                (ReviewStatus.NO_MATCH, "", b"", ""),
                (ReviewStatus.MATCHED, "4", b"four", ""),
            ]
        )

        result = await service.search(
            SimpleNamespace(unified_msg_origin="test"),
            "?? ????",
            "???? ???? ??",
        )

        self.assertEqual(result.image_bytes, b"four")
        self.assertEqual(result.review_status, ReviewStatus.MATCHED)
        self.assertEqual(result.reviewed_count, 4)
        self.assertEqual(service._vlm_selection.await_count, 2)

    async def test_all_batches_rejected_returns_no_image(self) -> None:
        service = self._service()
        service._vlm_selection = AsyncMock(
            return_value=(ReviewStatus.NO_MATCH, "", b"", "")
        )

        result = await service.search(
            SimpleNamespace(unified_msg_origin="test"),
            "?? ????",
            "???? ???? ??",
        )

        self.assertIsNone(result.image_bytes)
        self.assertEqual(result.review_status, ReviewStatus.NO_MATCH)
        self.assertEqual(result.reviewed_count, 6)
        self.assertIn("???????", result.error)

    async def test_review_error_keeps_candidate_only_for_fail_open_decision(self) -> None:
        service = self._service()
        service._vlm_selection = AsyncMock(
            return_value=(ReviewStatus.ERROR, "", b"", "????")
        )

        result = await service.search(
            SimpleNamespace(unified_msg_origin="test"),
            "?? ????",
            "???? ???? ??",
        )

        self.assertEqual(result.image_bytes, b"one")
        self.assertEqual(result.review_status, ReviewStatus.ERROR)
        self.assertTrue(result.review_fallback)

    async def test_second_review_round_prefers_bing_candidates(self) -> None:
        service = SoutuSearchService(
            _Context(),
            {
                "batch_size": 2,
                "review_rounds": 2,
                "bing_fallback_enabled": True,
            },
        )
        service._collect_url_pools = AsyncMock(
            return_value=(["p1", "p2", "p3", "p4"], [], "")
        )
        service._ensure_bing_pool = AsyncMock(
            side_effect=lambda _keyword, _target, pool, _loaded: (
                pool.extend(["b1", "b2"]) or True
            )
        )

        async def download(pool, target_count, _min_res, _hashes):
            chosen = list(pool[:target_count])
            del pool[:target_count]
            return [(url, url.encode()) for url in chosen]

        service._download_valid_batch = AsyncMock(side_effect=download)
        service._format_image = AsyncMock(side_effect=lambda value: value)
        service._vlm_selection = AsyncMock(
            side_effect=[
                (ReviewStatus.NO_MATCH, "", b"", ""),
                (ReviewStatus.MATCHED, "b2", b"b2", ""),
            ]
        )

        result = await service.search(
            SimpleNamespace(unified_msg_origin="test"),
            "??",
            "????????",
        )

        second_items = service._vlm_selection.await_args_list[1].args[1]
        self.assertEqual([url for url, _ in second_items], ["b1", "b2"])
        self.assertEqual(result.image_url, "b2")


class SoutuComposerTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_batch_restores_search_result_order(self) -> None:
        manager = ComposerManager()

        async def download(url):
            delays = {"first": 0.02, "second": 0.01, "third": 0}
            import asyncio

            await asyncio.sleep(delays[url])
            return url, url.encode()

        manager._download_image = download

        items = await manager.download_image_batch(
            ["first", "second", "third"], target_count=3
        )

        self.assertEqual([url for url, _ in items], ["first", "second", "third"])


class SerpApiReviewTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _service() -> SerpApiForwardService:
        service = SerpApiForwardService(
            _Context(),
            {"serpapi_keys": ["test-key"], "scrape_count": 2},
        )
        service._get_vlm_provider = AsyncMock(return_value=object())
        return service

    async def test_no_match_is_distinct_from_review_error(self) -> None:
        service = self._service()
        download = AsyncMock(return_value=b"first")
        with (
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service.fetch_image_urls",
                AsyncMock(return_value=["one", "two"]),
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service.run_tournament",
                AsyncMock(return_value=None),
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service.download_image",
                download,
            ),
        ):
            result = await service.search(
                SimpleNamespace(unified_msg_origin="test"),
                "??",
                "????????",
            )
        await service.close()

        self.assertEqual(result.review_status, ReviewStatus.NO_MATCH)
        self.assertTrue(result.review_fallback)
        self.assertIsNone(result.image_bytes)
        download.assert_not_awaited()

    async def test_review_error_is_marked_as_error(self) -> None:
        service = self._service()
        with (
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service.fetch_image_urls",
                AsyncMock(return_value=["one", "two"]),
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service.run_tournament",
                AsyncMock(side_effect=VlmReviewError("timeout")),
            ),
            patch(
                "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service.download_image",
                AsyncMock(return_value=b"first"),
            ),
        ):
            result = await service.search(
                SimpleNamespace(unified_msg_origin="test"),
                "??",
                "????????",
            )
        await service.close()

        self.assertEqual(result.review_status, ReviewStatus.ERROR)
        self.assertTrue(result.review_fallback)


class VlmParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_empty_selection_is_a_valid_no_match(self) -> None:
        provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(completion_text='{"selected_indices": []}')
            )
        )

        selected = await select_from_collage(b"image", "??", 2, provider, 1)

        self.assertEqual(selected, [])

    async def test_unparseable_response_is_a_review_error(self) -> None:
        provider = SimpleNamespace(
            text_chat=AsyncMock(return_value=SimpleNamespace(completion_text="???"))
        )
        with patch(
            "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.vlm.asyncio.sleep",
            AsyncMock(),
        ):
            with self.assertRaises(VlmReviewError):
                await select_from_collage(b"image", "??", 2, provider, 1)

    async def test_nonempty_invalid_indices_are_review_errors(self) -> None:
        for response_text in (
            '{"selected_indices": [99]}',
            '{"selected_indices": [0]}',
            '{"selected_indices": ["abc"]}',
        ):
            with self.subTest(response=response_text):
                provider = SimpleNamespace(
                    text_chat=AsyncMock(
                        return_value=SimpleNamespace(completion_text=response_text)
                    )
                )
                with patch(
                    "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.vlm.asyncio.sleep",
                    AsyncMock(),
                ):
                    with self.assertRaises(VlmReviewError):
                        await select_from_collage(b"image", "??", 2, provider, 1)


if __name__ == "__main__":
    unittest.main()
