from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_alice_image_assistant.alice_image.reverse import (
    controller as controller_module,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.ascii2d_strategy import (
    Ascii2dStrategy,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.constant import (
    DEFAULT_MAX_RESULTS,
    REVERSE_SEARCH_COMMAND,
    SOURCE_KEY_ASCII2D_BOVW,
    SOURCE_KEY_ASCII2D_COLOR,
    SOURCE_KEY_GOOGLE_LENS,
    SOURCE_KEY_SAUCENAO,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.controller import (
    AliceReverseController,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.google_lens_strategy import (
    GoogleLensStrategy,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.models import (
    SearchResultItem,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.ranking import (
    dedupe_by_url,
    merge_and_rank,
    normalize_url,
    positional_score,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.sauce_nao_strategy import (
    SauceNaoStrategy,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.utils import coerce_int
from astrbot_plugin_alice_image_assistant.alice_image.reverse.yandex_strategy import (
    YandexStrategy,
)


def _item(
    url: str,
    source: str,
    source_key: str,
    score: float | None = None,
    title: str = "t",
    thumbnail: str = "",
) -> SearchResultItem:
    """构造测试用结果项."""
    return SearchResultItem(
        title=title,
        url=url,
        thumbnail=thumbnail,
        source=source,
        score=score,
        source_key=source_key,
    )


class NormalizeUrlTests(unittest.TestCase):
    def test_scheme_host_and_trailing_slash_are_unified(self) -> None:
        self.assertEqual(
            normalize_url("HTTPS://WWW.Pixiv.NET/artworks/123/"),
            "https://pixiv.net/artworks/123",
        )

    def test_default_ports_are_dropped(self) -> None:
        self.assertEqual(
            normalize_url("https://pixiv.net:443/artworks/123"),
            "https://pixiv.net/artworks/123",
        )

    def test_tracking_params_are_stripped_and_rest_sorted(self) -> None:
        self.assertEqual(
            normalize_url(
                "https://example.com/p?utm_source=x&b=2&ref=twitter&a=1&from=qq"
            ),
            "https://example.com/p?a=1&b=2",
        )

    def test_fragment_is_dropped(self) -> None:
        self.assertEqual(
            normalize_url("https://example.com/p#comments"),
            "https://example.com/p",
        )

    def test_same_page_from_two_engines_collapses_to_one_key(self) -> None:
        first = normalize_url("https://www.pixiv.net/artworks/999?utm_medium=lens")
        second = normalize_url("https://pixiv.net/artworks/999/")
        self.assertEqual(first, second)

    def test_relative_and_empty_urls_degrade_safely(self) -> None:
        self.assertEqual(normalize_url(""), "")
        self.assertEqual(normalize_url("   "), "")
        # 相对链接无法规范化，退化成小写原串即可，至少还能自身去重
        self.assertEqual(normalize_url("/search/color/abc"), "/search/color/abc")


class PositionalScoreTests(unittest.TestCase):
    def test_first_position_gets_full_source_confidence(self) -> None:
        self.assertAlmostEqual(
            positional_score(0, 5, SOURCE_KEY_GOOGLE_LENS), 0.6, places=6
        )

    def test_score_decreases_with_position(self) -> None:
        scores = [positional_score(i, 4, SOURCE_KEY_GOOGLE_LENS) for i in range(4)]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertGreater(scores[0], scores[-1])

    def test_bovw_outweighs_color_at_the_same_position(self) -> None:
        # bovw 是特征匹配，比 color 配色匹配更可信，同位次必须分数更高
        self.assertGreater(
            positional_score(0, 3, SOURCE_KEY_ASCII2D_BOVW),
            positional_score(0, 3, SOURCE_KEY_ASCII2D_COLOR),
        )

    def test_degenerate_inputs_return_zero(self) -> None:
        self.assertEqual(positional_score(0, 0, SOURCE_KEY_ASCII2D_BOVW), 0.0)
        self.assertEqual(positional_score(-1, 3, SOURCE_KEY_ASCII2D_BOVW), 0.0)


class CoerceIntTests(unittest.TestCase):
    def test_string_numbers_are_accepted(self) -> None:
        self.assertEqual(coerce_int("40", 60, 0, 100), 40)
        self.assertEqual(coerce_int(" 7 ", 5, 1, 30), 7)

    def test_garbage_falls_back_to_default(self) -> None:
        self.assertEqual(coerce_int(None, 60, 0, 100), 60)
        self.assertEqual(coerce_int("abc", 60, 0, 100), 60)
        self.assertEqual(coerce_int({}, 60, 0, 100), 60)
        # bool 是 int 子类，但把 True 当 1 几乎总是配置写错
        self.assertEqual(coerce_int(True, 60, 0, 100), 60)

    def test_values_are_clamped(self) -> None:
        self.assertEqual(coerce_int("999", 60, 0, 100), 100)
        self.assertEqual(coerce_int("-5", 60, 0, 100), 0)


class DedupeByUrlTests(unittest.TestCase):
    def test_bovw_wins_over_color_for_the_same_url(self) -> None:
        bovw = _item(
            "https://www.pixiv.net/artworks/1",
            "Ascii2d",
            SOURCE_KEY_ASCII2D_BOVW,
            score=0.85,
        )
        color = _item(
            "https://pixiv.net/artworks/1/?utm_source=x",
            "Ascii2d",
            SOURCE_KEY_ASCII2D_COLOR,
            score=0.7,
        )
        merged = dedupe_by_url([bovw, color])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source_key, SOURCE_KEY_ASCII2D_BOVW)
        self.assertAlmostEqual(merged[0].score or 0.0, 0.85, places=6)

    def test_higher_color_score_is_kept_on_the_bovw_item(self) -> None:
        bovw = _item("https://a.test/x", "Ascii2d", SOURCE_KEY_ASCII2D_BOVW, score=0.3)
        color = _item("https://a.test/x", "Ascii2d", SOURCE_KEY_ASCII2D_COLOR, score=0.6)
        merged = dedupe_by_url([bovw, color])

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source_key, SOURCE_KEY_ASCII2D_BOVW)
        self.assertAlmostEqual(merged[0].score or 0.0, 0.6, places=6)

    def test_missing_thumbnail_is_borrowed(self) -> None:
        first = _item("https://a.test/x", "Ascii2d", SOURCE_KEY_ASCII2D_BOVW, score=0.5)
        second = _item(
            "https://a.test/x",
            "Ascii2d",
            SOURCE_KEY_ASCII2D_COLOR,
            score=0.4,
            thumbnail="https://a.test/thumb.jpg",
        )
        merged = dedupe_by_url([first, second])

        self.assertEqual(merged[0].thumbnail, "https://a.test/thumb.jpg")

    def test_distinct_urls_are_kept_in_order(self) -> None:
        items = [
            _item("https://a.test/1", "Ascii2d", SOURCE_KEY_ASCII2D_BOVW, score=0.8),
            _item("https://a.test/2", "Ascii2d", SOURCE_KEY_ASCII2D_BOVW, score=0.6),
        ]
        merged = dedupe_by_url(items)
        self.assertEqual([i.url for i in merged], ["https://a.test/1", "https://a.test/2"])

    def test_empty_urls_are_not_collapsed_together(self) -> None:
        items = [
            _item("", "Ascii2d", SOURCE_KEY_ASCII2D_BOVW),
            _item("", "Ascii2d", SOURCE_KEY_ASCII2D_BOVW),
        ]
        self.assertEqual(len(dedupe_by_url(items)), 2)


class MergeAndRankTests(unittest.TestCase):
    def test_saucenao_hit_outranks_earlier_google_results(self) -> None:
        items = [
            _item("https://g.test/1", "Google Lens", SOURCE_KEY_GOOGLE_LENS, score=0.6),
            _item("https://g.test/2", "Google Lens", SOURCE_KEY_GOOGLE_LENS, score=0.45),
            _item("https://s.test/1", "SauceNAO", SOURCE_KEY_SAUCENAO, score=0.95),
        ]
        ranked = merge_and_rank(items, limit=5)

        self.assertEqual(ranked[0].url, "https://s.test/1")

    def test_unscored_results_sink_to_the_bottom(self) -> None:
        items = [
            _item("https://x.test/none", "Unknown", ""),
            _item("https://x.test/scored", "SauceNAO", SOURCE_KEY_SAUCENAO, score=0.61),
        ]
        ranked = merge_and_rank(items, limit=5)

        self.assertEqual([i.url for i in ranked], ["https://x.test/scored", "https://x.test/none"])

    def test_cross_engine_consensus_boosts_score_and_records_engines(self) -> None:
        items = [
            _item(
                "https://www.pixiv.net/artworks/7/",
                "SauceNAO",
                SOURCE_KEY_SAUCENAO,
                score=0.7,
            ),
            _item("https://other.test/a", "SauceNAO", SOURCE_KEY_SAUCENAO, score=0.7),
            _item(
                "https://pixiv.net/artworks/7?ref=lens",
                "Ascii2D",
                SOURCE_KEY_ASCII2D_BOVW,
                score=0.5,
            ),
        ]
        ranked = merge_and_rank(items, limit=5)

        self.assertEqual(len(ranked), 2)
        top = ranked[0]
        self.assertEqual(top.source, "SauceNAO")
        self.assertEqual(top.matched_by, ["SauceNAO", "Ascii2D"])
        # 多引擎共识是最强的准确性信号，同分时必须被抬到前面
        self.assertGreater(top.score or 0.0, 0.7)
        self.assertEqual(ranked[1].matched_by, ["SauceNAO"])

    def test_consensus_bonus_is_capped_and_never_exceeds_one(self) -> None:
        items = [
            _item("https://a.test/x", "SauceNAO", SOURCE_KEY_SAUCENAO, score=0.99),
            _item("https://a.test/x", "Ascii2D", SOURCE_KEY_ASCII2D_BOVW, score=0.5),
            _item("https://a.test/x", "Google Lens", SOURCE_KEY_GOOGLE_LENS, score=0.4),
        ]
        ranked = merge_and_rank(items, limit=5)

        self.assertEqual(len(ranked), 1)
        self.assertLessEqual(ranked[0].score or 0.0, 1.0)
        self.assertEqual(len(ranked[0].matched_by), 3)

    def test_ranking_happens_before_truncation(self) -> None:
        items = [
            _item("https://g.test/1", "Google Lens", SOURCE_KEY_GOOGLE_LENS, score=0.6),
            _item("https://g.test/2", "Google Lens", SOURCE_KEY_GOOGLE_LENS, score=0.5),
            _item("https://s.test/1", "SauceNAO", SOURCE_KEY_SAUCENAO, score=0.98),
        ]
        ranked = merge_and_rank(items, limit=1)

        self.assertEqual([i.url for i in ranked], ["https://s.test/1"])

    def test_equal_scores_fall_back_to_source_priority_then_position(self) -> None:
        items = [
            _item("https://g.test/1", "Google Lens", SOURCE_KEY_GOOGLE_LENS, score=0.5),
            _item("https://s.test/1", "SauceNAO", SOURCE_KEY_SAUCENAO, score=0.5),
            _item("https://s.test/2", "SauceNAO", SOURCE_KEY_SAUCENAO, score=0.5),
        ]
        ranked = merge_and_rank(items, limit=5)

        self.assertEqual(
            [i.url for i in ranked],
            ["https://s.test/1", "https://s.test/2", "https://g.test/1"],
        )

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual(merge_and_rank([], limit=5), [])


def _ascii2d_item_box(thumb: str, detail_inner: str) -> str:
    """构造一个 ascii2d item-box HTML 片段."""
    return (
        "<div class='row item-box'>\n"
        "  <div class='col-md-8 text-xs-center image-box'>\n"
        f"    <img src='{thumb}' class='image-box'>\n"
        "  </div>\n"
        "  <div class='col-md-4 info-box'>\n"
        f"    <div class='detail-box gray-link'>{detail_inner}</div>\n"
        "  </div>\n"
        "  <div class='clearfix'></div>\n"
        "</div>\n"
    )


ASCII2D_ORIGINAL_BOX = _ascii2d_item_box(
    "/thumbnail/0/0/0/0/original.jpg",
    "<small>1000x1000 JPEG 120.5KB</small>",
)
ASCII2D_FIRST_BOX = _ascii2d_item_box(
    "/thumbnail/a/a/a/a/first.jpg",
    "<h6><img src='/assets/pixiv.ico'>"
    "<a href='https://www.pixiv.net/artworks/111' target='_blank'>作品一</a>"
    "<small><a href='https://www.pixiv.net/users/11' target='_blank'>画师一</a></small>"
    "</h6>",
)
ASCII2D_SECOND_BOX = _ascii2d_item_box(
    "/thumbnail/b/b/b/b/second.jpg",
    "<h6><img src='/assets/twitter.ico'>"
    "<a href='https://twitter.com/someone/status/222' target='_blank'>作品二</a>"
    "</h6>",
)


class Ascii2dParsingTests(unittest.TestCase):
    def test_query_box_is_skipped_by_content_not_by_index(self) -> None:
        html = ASCII2D_ORIGINAL_BOX + ASCII2D_FIRST_BOX + ASCII2D_SECOND_BOX
        results = Ascii2dStrategy._parse_ascii2d_html(html, SOURCE_KEY_ASCII2D_BOVW)

        self.assertEqual([r.title for r in results], ["作品一", "作品二"])

    def test_first_hit_is_kept_when_query_box_is_absent(self) -> None:
        # 原来无条件丢弃第一个 item-box，正则漏匹配时会丢掉最相关的首条命中
        html = ASCII2D_FIRST_BOX + ASCII2D_SECOND_BOX
        results = Ascii2dStrategy._parse_ascii2d_html(html, SOURCE_KEY_ASCII2D_BOVW)

        self.assertEqual([r.title for r in results], ["作品一", "作品二"])

    def test_thumb_link_and_title_stay_bound_to_the_same_box(self) -> None:
        html = ASCII2D_ORIGINAL_BOX + ASCII2D_FIRST_BOX + ASCII2D_SECOND_BOX
        results = Ascii2dStrategy._parse_ascii2d_html(html, SOURCE_KEY_ASCII2D_BOVW)

        self.assertEqual(results[0].url, "https://www.pixiv.net/artworks/111")
        self.assertEqual(
            results[0].thumbnail, "https://ascii2d.net/thumbnail/a/a/a/a/first.jpg"
        )
        self.assertEqual(results[1].url, "https://twitter.com/someone/status/222")
        self.assertEqual(
            results[1].thumbnail, "https://ascii2d.net/thumbnail/b/b/b/b/second.jpg"
        )

    def test_scores_and_source_key_are_written(self) -> None:
        html = ASCII2D_FIRST_BOX + ASCII2D_SECOND_BOX
        results = Ascii2dStrategy._parse_ascii2d_html(html, SOURCE_KEY_ASCII2D_COLOR)

        self.assertTrue(all(r.source_key == SOURCE_KEY_ASCII2D_COLOR for r in results))
        self.assertIsNotNone(results[0].score)
        self.assertGreater(results[0].score or 0.0, results[1].score or 0.0)

    def test_site_icon_is_not_used_as_thumbnail(self) -> None:
        box = (
            "<div class='row item-box'>"
            "<div class='info-box'>"
            "<div class='detail-box'><h6><img src='/assets/pixiv.ico'>"
            "<a href='https://www.pixiv.net/artworks/333'>作品三</a></h6></div>"
            "</div>"
            "<img src='/thumbnail/c/c/c/c/third.jpg'>"
            "<div class='clearfix'></div>"
            "</div>"
        )
        results = Ascii2dStrategy._parse_ascii2d_html(box, SOURCE_KEY_ASCII2D_BOVW)

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].thumbnail, "https://ascii2d.net/thumbnail/c/c/c/c/third.jpg"
        )

    def test_bovw_and_color_overlap_is_deduped(self) -> None:
        html = ASCII2D_ORIGINAL_BOX + ASCII2D_FIRST_BOX + ASCII2D_SECOND_BOX
        bovw = Ascii2dStrategy._parse_ascii2d_html(html, SOURCE_KEY_ASCII2D_BOVW)
        color = Ascii2dStrategy._parse_ascii2d_html(html, SOURCE_KEY_ASCII2D_COLOR)

        combined = dedupe_by_url([*bovw, *color])

        self.assertEqual(len(combined), 2)
        self.assertTrue(all(r.source_key == SOURCE_KEY_ASCII2D_BOVW for r in combined))


class _FakeResponse:
    """模拟 aiohttp 响应."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def text(self) -> str:
        return json.dumps(self._payload)


class _FakeRequest:
    """模拟 aiohttp 的请求上下文管理器 (session.get(...) 的返回值)."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeSession:
    """模拟 aiohttp 会话，只实现 get()."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def get(self, *_args: object, **_kwargs: object) -> _FakeRequest:
        return _FakeRequest(_FakeResponse(self._payload))


def _sauce_node(similarity: object, ext_urls: object, thumbnail: str = "") -> dict:
    """构造 SauceNAO 结果节点."""
    return {
        "header": {"similarity": similarity, "thumbnail": thumbnail},
        "data": {"title": f"title-{similarity}", "ext_urls": ext_urls},
    }


class SauceNaoParsingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.strategy = SauceNaoStrategy(api_key="key", similarity_threshold=60)

    def test_score_is_normalized_and_similarity_stays_readable(self) -> None:
        item = self.strategy._parse_node(
            _sauce_node("95.32", ["https://www.pixiv.net/artworks/1"])
        )

        self.assertIsNotNone(item)
        assert item is not None
        self.assertAlmostEqual(item.score or 0.0, 0.9532, places=6)
        self.assertEqual(item.similarity, "95.32%")
        self.assertEqual(item.source_key, SOURCE_KEY_SAUCENAO)

    def test_low_similarity_is_filtered(self) -> None:
        self.assertIsNone(
            self.strategy._parse_node(_sauce_node(41, ["https://a.test/1"]))
        )

    def test_missing_ext_urls_falls_back_to_thumbnail(self) -> None:
        item = self.strategy._parse_node(
            _sauce_node(90, [], thumbnail="https://img.test/t.jpg")
        )

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.url, "https://img.test/t.jpg")

    def test_result_without_any_link_is_skipped(self) -> None:
        self.assertIsNone(self.strategy._parse_node(_sauce_node(90, [])))
        self.assertIsNone(self.strategy._parse_node(_sauce_node(90, "not-a-list")))

    def test_dict_similarity_raises_for_per_item_handling(self) -> None:
        with self.assertRaises(TypeError):
            self.strategy._parse_node(_sauce_node({"bad": 1}, ["https://a.test/1"]))

    async def test_one_dirty_node_does_not_truncate_the_rest(self) -> None:
        payload = {
            "results": [
                _sauce_node("95", ["https://a.test/1"]),
                _sauce_node({"bad": 1}, ["https://a.test/2"]),
                _sauce_node("80", ["https://a.test/3"]),
            ]
        }
        with patch(
            "astrbot_plugin_alice_image_assistant.alice_image.reverse."
            "sauce_nao_strategy.get_aiohttp_session",
            AsyncMock(return_value=_FakeSession(payload)),
        ):
            results = await self.strategy.search("https://example.com/input.jpg")

        # 坏数据只跳过它自己，不能让异常逃到外层把后续结果全部静默截断
        self.assertEqual([r.url for r in results], ["https://a.test/1", "https://a.test/3"])

    async def test_non_list_results_are_ignored(self) -> None:
        with patch(
            "astrbot_plugin_alice_image_assistant.alice_image.reverse."
            "sauce_nao_strategy.get_aiohttp_session",
            AsyncMock(return_value=_FakeSession({"results": "oops"})),
        ):
            results = await self.strategy.search("https://example.com/input.jpg")

        self.assertEqual(results, [])


class GoogleLensKeyRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_advances_between_selections(self) -> None:
        strategy = GoogleLensStrategy(api_keys=["key-aaaa", "key-bbbb"])

        first = await strategy._select_key_optimistically()
        second = await strategy._select_key_optimistically()

        # 游标不前移时多 Key 负载均衡会完全失效（同一次搜图重试反复挑同一个坏 Key）
        self.assertNotEqual(first, second)
        self.assertEqual({first, second}, {"key-aaaa", "key-bbbb"})

    async def test_penalized_key_is_skipped(self) -> None:
        strategy = GoogleLensStrategy(api_keys=["key-aaaa", "key-bbbb"])
        await strategy._penalize_key("key-aaaa")

        for _ in range(3):
            self.assertEqual(await strategy._select_key_optimistically(), "key-bbbb")

    async def test_all_penalized_keys_still_yield_a_candidate(self) -> None:
        strategy = GoogleLensStrategy(api_keys=["key-aaaa", "key-bbbb"])
        await strategy._penalize_key("key-aaaa")
        await strategy._penalize_key("key-bbbb")

        # 惩罚只是降级而不是拉黑，全部冷却时仍要给出一个 Key 而不是彻底不搜
        self.assertIn(await strategy._select_key_optimistically(), strategy.api_keys)

    async def test_no_keys_returns_none(self) -> None:
        self.assertIsNone(await GoogleLensStrategy(api_keys=[])._select_key_optimistically())

    async def test_failed_request_penalizes_the_key(self) -> None:
        strategy = GoogleLensStrategy(api_keys=["key-aaaa", "key-bbbb"])

        async def _boom(_api_key: str, _image_url: str) -> list[SearchResultItem]:
            raise RuntimeError("network down")

        strategy._request_with_key = _boom  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            await strategy._search_with_key("https://example.com/a.jpg")

        self.assertEqual(len(strategy._key_penalty_until), 1)


def _event(session_id: str = "session-1") -> SimpleNamespace:
    """构造最小消息事件桩."""
    return SimpleNamespace(session_id=session_id)


class _NoopStrategy:
    """占位策略，只为让 controller 认为"有可用引擎"."""

    def get_service_name(self) -> str:
        return "Noop"

    async def search(self, _image_url: str) -> list[SearchResultItem]:
        return []

    async def close(self) -> None:
        return None


class ControllerConfigRobustnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_dict_config_sections_do_not_crash_init(self) -> None:
        controller = AliceReverseController(
            Mock(),
            {
                "network": "http://127.0.0.1:7890",
                "ai_behavior": ["oops"],
                "strategies": "enable_all",
                "api_keys": 123,
                "display": "5",
            },
        )
        try:
            self.assertEqual(len(controller.strategies), 1)
            self.assertIsInstance(controller.strategies[0], YandexStrategy)
            self.assertEqual(controller.service.max_results, DEFAULT_MAX_RESULTS)
        finally:
            await controller.terminate()

    async def test_string_similarity_threshold_does_not_crash_init(self) -> None:
        controller = AliceReverseController(
            Mock(),
            {
                "strategies": {
                    "enable_saucenao": True,
                    "enable_google_lens": False,
                    "enable_ascii2d": False,
                    "enable_yandex": False,
                    "saucenao_similarity_threshold": "40",
                    "saucenao_numres": "9",
                },
                "api_keys": {"saucenao_api_key": "sauce-key"},
                "display": {"max_results": "3"},
                "network": {"total_timeout_seconds": "20"},
            },
        )
        try:
            self.assertEqual(len(controller.strategies), 1)
            strategy = controller.strategies[0]
            self.assertIsInstance(strategy, SauceNaoStrategy)
            assert isinstance(strategy, SauceNaoStrategy)
            self.assertEqual(strategy.similarity_threshold, 40)
            self.assertEqual(strategy.max_results, 9)
            self.assertEqual(controller.service.max_results, 3)
            self.assertEqual(controller.service.total_timeout_seconds, 20)
        finally:
            await controller.terminate()

    def test_command_names_come_from_constants(self) -> None:
        event = SimpleNamespace(
            is_at_or_wake_command=True,
            message_str=f"{REVERSE_SEARCH_COMMAND} google",
        )
        self.assertTrue(AliceReverseController._is_search_command_event(event))

        alias_event = SimpleNamespace(
            is_at_or_wake_command=True,
            message_str="aa溯图 google",
        )
        self.assertFalse(AliceReverseController._is_search_command_event(alias_event))

        # 命令名与别名统一从常量读取：补别名后无需再改判断逻辑
        with patch.object(
            controller_module,
            "REVERSE_SEARCH_COMMAND_NAMES",
            (REVERSE_SEARCH_COMMAND, "aa溯图"),
        ):
            self.assertTrue(AliceReverseController._is_search_command_event(alias_event))


class ControllerImageIdTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_image_id_returns_error_instead_of_latest_image(self) -> None:
        controller = AliceReverseController(
            Mock(),
            {
                "strategies": {
                    "enable_saucenao": False,
                    "enable_google_lens": False,
                    "enable_ascii2d": False,
                    "enable_yandex": False,
                }
            },
        )
        controller.strategies.append(_NoopStrategy())  # type: ignore[arg-type]
        try:
            payload = json.loads(
                await controller.tool_search_image(_event(), image_id="does-not-exist")
            )
        finally:
            await controller.terminate()

        # 静默回退到最新图片会让 LLM 搜错图还以为搜对了
        self.assertFalse(payload["success"])
        self.assertIn("does-not-exist", payload["error"])
        self.assertIn("image_context", payload)


if __name__ == "__main__":
    unittest.main()
