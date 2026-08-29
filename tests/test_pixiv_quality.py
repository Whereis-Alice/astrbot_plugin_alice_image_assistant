from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils import query_plan as qp
from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.tag import (
    FilterConfig,
    _extract_tag_names,
    _is_ai_by_field,
    _is_below_like_threshold,
    has_excluded_tags,
    item_has_any_exact_tag,
    sample_illusts,
)


def _tag(name: str, translated: str | None = None) -> SimpleNamespace:
    """构造一个 Pixiv tag 对象。"""
    return SimpleNamespace(name=name, translated_name=translated)


def _illust(
    illust_id: int,
    bookmarks: int = 0,
    tags: list | None = None,
) -> SimpleNamespace:
    """构造一个精简的插画对象。"""
    return SimpleNamespace(
        id=illust_id,
        total_bookmarks=bookmarks,
        tags=tags if tags is not None else [],
    )


class AutocompleteExtractionTests(unittest.TestCase):
    """search_autocomplete 结果解析。"""

    def test_prefers_name_and_dedupes(self) -> None:
        result = SimpleNamespace(
            tags=[
                _tag("初音ミク", "hatsune miku"),
                _tag("初音ミク", "hatsune miku"),
                _tag("VOCALOID", "vocaloid"),
            ]
        )
        self.assertEqual(
            qp.extract_autocomplete_tags(result, limit=3), ["初音ミク", "VOCALOID"]
        )

    def test_supports_dict_payload(self) -> None:
        result = {"tags": [{"name": "", "translated_name": "genshin"}]}
        self.assertEqual(qp.extract_autocomplete_tags(result), ["genshin"])

    def test_bad_payload_returns_empty(self) -> None:
        self.assertEqual(qp.extract_autocomplete_tags(None), [])
        self.assertEqual(qp.extract_autocomplete_tags(SimpleNamespace()), [])
        self.assertEqual(qp.extract_autocomplete_tags({"tags": "oops"}), [])

    def test_limit_is_respected(self) -> None:
        result = SimpleNamespace(tags=[_tag("a"), _tag("b"), _tag("c")])
        self.assertEqual(qp.extract_autocomplete_tags(result, limit=2), ["a", "b"])
        self.assertEqual(qp.extract_autocomplete_tags(result, limit=0), [])


class SearchPlanTests(unittest.TestCase):
    """多级搜索计划生成。"""

    def test_empty_query_yields_empty_plan(self) -> None:
        self.assertEqual(qp.build_search_plan("   "), [])

    def test_plan_prefers_normalized_exact_then_falls_back(self) -> None:
        plan = qp.build_search_plan(
            "初音",
            normalized_tags=["初音ミク"],
            options=qp.QueryPlanOptions(max_steps=10),
        )
        targets = [(step.word, step.search_target) for step in plan]
        self.assertEqual(targets[0], ("初音ミク", qp.SEARCH_TARGET_EXACT))
        self.assertEqual(targets[1], ("初音", qp.SEARCH_TARGET_EXACT))
        self.assertEqual(targets[2], ("初音ミク", qp.SEARCH_TARGET_PARTIAL))
        self.assertEqual(targets[3], ("初音", qp.SEARCH_TARGET_PARTIAL))
        self.assertEqual(targets[4], ("初音", qp.SEARCH_TARGET_TITLE_CAPTION))

    def test_plan_without_normalized_tags_matches_legacy_behaviour(self) -> None:
        """autocomplete 失败（无归一化 tag）时必须仍保留旧的 partial 行为。"""
        plan = qp.build_search_plan("原神", options=qp.QueryPlanOptions(max_steps=10))
        self.assertIn(
            ("原神", qp.SEARCH_TARGET_PARTIAL),
            [(s.word, s.search_target) for s in plan],
        )
        fallbacks = [s for s in plan if s.is_fallback]
        self.assertTrue(fallbacks)

    def test_max_steps_keeps_fallback_step(self) -> None:
        plan = qp.build_search_plan(
            "初音",
            normalized_tags=["初音ミク", "VOCALOID"],
            options=qp.QueryPlanOptions(max_steps=2),
        )
        self.assertEqual(len(plan), 2)
        self.assertTrue(any(step.is_fallback for step in plan))

    def test_disabled_switches(self) -> None:
        options = qp.QueryPlanOptions(
            enable_exact=False, enable_title_caption=False, max_steps=10
        )
        plan = qp.build_search_plan("原神", normalized_tags=["原神"], options=options)
        self.assertEqual(
            [(s.word, s.search_target) for s in plan],
            [("原神", qp.SEARCH_TARGET_PARTIAL)],
        )

    def test_normalized_tag_equal_to_query_is_not_duplicated(self) -> None:
        plan = qp.build_search_plan(
            "原神", normalized_tags=["原神"], options=qp.QueryPlanOptions(max_steps=10)
        )
        words = [(s.word, s.search_target) for s in plan]
        self.assertEqual(len(words), len(set(words)))

    def test_to_search_kwargs(self) -> None:
        step = qp.SearchStep(word="原神", sort=qp.SORT_POPULAR_DESC)
        kwargs = step.to_search_kwargs(extra={"duration": None, "req_auth": True})
        self.assertEqual(
            kwargs,
            {
                "word": "原神",
                "search_target": qp.SEARCH_TARGET_PARTIAL,
                "sort": qp.SORT_POPULAR_DESC,
                "filter": "for_ios",
                "req_auth": True,
            },
        )

    def test_has_enough_results(self) -> None:
        self.assertTrue(qp.has_enough_results(10, 10))
        self.assertFalse(qp.has_enough_results(9, 10))
        self.assertFalse(qp.has_enough_results(None, 10))


class OptionsFromConfigTests(unittest.TestCase):
    """配置读取必须对缺失字段安全（不依赖 schema）。"""

    def test_defaults_when_config_missing(self) -> None:
        options = qp.options_from_config(None)
        self.assertTrue(options.enable_autocomplete)
        self.assertTrue(options.enable_exact)
        self.assertTrue(options.enable_title_caption)
        self.assertEqual(options.max_steps, qp.DEFAULT_MAX_STEPS)

    def test_reads_from_namespace(self) -> None:
        config = SimpleNamespace(
            search_autocomplete_enabled=False,
            search_plan_max_steps=2,
            search_autocomplete_limit=5,
        )
        options = qp.options_from_config(config)
        self.assertFalse(options.enable_autocomplete)
        self.assertEqual(options.max_steps, 2)
        self.assertEqual(options.autocomplete_limit, 5)

    def test_reads_from_dict_and_string_bools(self) -> None:
        options = qp.options_from_config(
            {
                "search_title_caption_fallback": "false",
                "search_prefer_popular": "关闭",
                "search_plan_min_results": "20",
            }
        )
        self.assertFalse(options.enable_title_caption)
        self.assertFalse(options.prefer_popular)
        self.assertEqual(options.min_results, 20)

    def test_invalid_numbers_fall_back_to_defaults(self) -> None:
        options = qp.options_from_config({"search_plan_max_steps": "abc"})
        self.assertEqual(options.max_steps, qp.DEFAULT_MAX_STEPS)


class ResultHelperTests(unittest.TestCase):
    """结果层工具函数。"""

    def test_dedupe_keeps_order(self) -> None:
        a, b = _illust(1), _illust(2)
        self.assertEqual(qp.dedupe_illusts([a, b, a, b]), [a, b])

    def test_dedupe_handles_missing_id(self) -> None:
        anonymous = SimpleNamespace(total_bookmarks=1)
        self.assertEqual(len(qp.dedupe_illusts([anonymous, anonymous])), 1)

    def test_sort_by_bookmarks_is_stable(self) -> None:
        low, high, same = _illust(1, 10), _illust(2, 900), _illust(3, 10)
        self.assertEqual(
            qp.sort_illusts_by_bookmarks([low, high, same]), [high, low, same]
        )

    def test_extract_illusts_and_next_url(self) -> None:
        result = SimpleNamespace(illusts=[_illust(1)], next_url="https://x/?offset=30")
        self.assertEqual(len(qp.extract_illusts(result)), 1)
        self.assertEqual(qp.extract_next_url(result), "https://x/?offset=30")
        self.assertEqual(qp.extract_illusts(None), [])
        self.assertEqual(qp.extract_illusts(SimpleNamespace()), [])
        self.assertIsNone(qp.extract_next_url(SimpleNamespace(next_url=None)))


class PopularDegradeDetectionTests(unittest.TestCase):
    """popular_desc 被静默降级为时间序的检测。"""

    def test_detects_time_ordered_results(self) -> None:
        illusts = [_illust(300, 5), _illust(200, 900), _illust(100, 30)]
        self.assertTrue(qp.detect_popular_desc_degraded(illusts))

    def test_true_popular_order_is_not_flagged(self) -> None:
        illusts = [_illust(100, 900), _illust(300, 500), _illust(200, 10)]
        self.assertFalse(qp.detect_popular_desc_degraded(illusts))

    def test_small_sample_is_never_flagged(self) -> None:
        self.assertFalse(qp.detect_popular_desc_degraded([_illust(2, 1), _illust(1, 9)]))

    def test_missing_ids_are_not_flagged(self) -> None:
        items = [
            SimpleNamespace(total_bookmarks=1),
            SimpleNamespace(total_bookmarks=9),
            SimpleNamespace(total_bookmarks=3),
        ]
        self.assertFalse(qp.detect_popular_desc_degraded(items))


class ApiErrorDescriptionTests(unittest.TestCase):
    """API 错误对象中文化。"""

    def test_no_error_returns_none(self) -> None:
        self.assertIsNone(qp.describe_api_error(SimpleNamespace(illusts=[], error=None)))

    def test_none_result_is_reported(self) -> None:
        self.assertIn("未返回", qp.describe_api_error(None) or "")

    def test_rate_limit_is_translated(self) -> None:
        result = {"error": {"message": "Rate Limit reached"}}
        self.assertIn("限流", qp.describe_api_error(result) or "")

    def test_invalid_grant_is_translated(self) -> None:
        result = SimpleNamespace(
            error=SimpleNamespace(user_message="", message="invalid_grant", reason="")
        )
        self.assertIn("refresh_token", qp.describe_api_error(result) or "")

    def test_unknown_error_keeps_detail_without_raw_traceback(self) -> None:
        message = qp.describe_api_error({"error": {"message": "something odd"}})
        self.assertEqual(message, "Pixiv API 返回错误：something odd")

    def test_empty_error_detail_still_reports_failure(self) -> None:
        # error 对象存在但没有任何可读文案时，仍要给出统一的中文提示而不是当成无错误。
        message = qp.describe_api_error({"error": {"message": ""}})
        self.assertEqual(message, "Pixiv API 返回了未知错误，请稍后再试。")

    def test_no_error_field_returns_none(self) -> None:
        self.assertIsNone(qp.describe_api_error({"illusts": []}))


class ExcludedTagWholeWordTests(unittest.TestCase):
    """排除标签必须整词匹配，不能子串误杀。"""

    def test_ai_does_not_kill_maid_or_waist(self) -> None:
        item = _illust(1, tags=[_tag("maid"), _tag("waist")])
        self.assertFalse(has_excluded_tags(item, ["ai"]))

    def test_ai_still_matches_whole_word(self) -> None:
        item = _illust(1, tags=[_tag("AI generated")])
        self.assertTrue(has_excluded_tags(item, ["ai"]))

    def test_exact_tag_match_still_works(self) -> None:
        item = _illust(1, tags=[_tag("R-18")])
        self.assertTrue(has_excluded_tags(item, ["r-18"]))

    def test_translated_name_is_considered(self) -> None:
        item = _illust(1, tags=[_tag("女の子", "少女")])
        self.assertTrue(has_excluded_tags(item, ["少女"]))

    def test_none_tag_names_do_not_raise(self) -> None:
        item = _illust(1, tags=[_tag(None, None), None])
        self.assertFalse(has_excluded_tags(item, ["ai"]))


class TagExtractionTests(unittest.TestCase):
    """标签抽取与精确匹配。"""

    def test_extract_includes_translated_names(self) -> None:
        names = _extract_tag_names([_tag("女の子", "girl")])
        self.assertIn("女の子", names)
        self.assertIn("girl", names)

    def test_item_has_any_exact_tag_handles_none(self) -> None:
        item = _illust(1, tags=[_tag(None, "genshin"), _tag("原神", None)])
        self.assertTrue(item_has_any_exact_tag(item, ["原神"]))
        self.assertTrue(item_has_any_exact_tag(item, "GENSHIN"))
        self.assertFalse(item_has_any_exact_tag(item, []))

    def test_is_ai_by_field_returns_bool(self) -> None:
        self.assertIs(_is_ai_by_field(SimpleNamespace(illust_ai_type=2)), True)
        self.assertIs(_is_ai_by_field(SimpleNamespace(illust_ai_type=1)), False)
        self.assertIs(_is_ai_by_field(SimpleNamespace()), False)


class SampleIllustsTests(unittest.TestCase):
    """采样不得污染调用方的候选列表顺序。"""

    def test_shuffle_does_not_mutate_input(self) -> None:
        items = [_illust(i) for i in range(20)]
        snapshot = list(items)
        sample_illusts(items, 5, shuffle=True)
        self.assertEqual(items, snapshot)

    def test_without_shuffle_returns_subset_without_mutating(self) -> None:
        items = [_illust(i) for i in range(5)]
        snapshot = list(items)
        picked = sample_illusts(items, 3)
        self.assertEqual(len(picked), 3)
        self.assertEqual(len({id(i) for i in picked}), 3)
        for item in picked:
            self.assertIn(item, items)
        self.assertEqual(items, snapshot)


class LikeThresholdTests(unittest.TestCase):
    """min_likes 依赖的字段 app-api 不返回，必须告警而非静默放过。"""

    def test_missing_like_field_warns_once_and_does_not_filter(self) -> None:
        from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils import (
            tag as tag_module,
        )

        tag_module._LIKE_FIELD_MISSING_WARNED = False
        logger = Mock()
        config = FilterConfig(
            r18_mode="不过滤", ai_filter_mode="不过滤", logger=logger
        )
        item = _illust(1)

        self.assertFalse(_is_below_like_threshold(item, 100, config))
        self.assertFalse(_is_below_like_threshold(item, 100, config))
        self.assertEqual(logger.warning.call_count, 1)

    def test_present_like_field_is_compared(self) -> None:
        item = SimpleNamespace(id=1, total_like=10, tags=[])
        config = FilterConfig(
            r18_mode="不过滤", ai_filter_mode="不过滤", logger=Mock()
        )
        self.assertTrue(_is_below_like_threshold(item, 100, config))
        self.assertFalse(_is_below_like_threshold(item, 5, config))


if __name__ == "__main__":
    unittest.main()
