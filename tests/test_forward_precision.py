"""forward 找图精准度改造的单元测试（A~F 项 + 缺陷 1/2/4）。

全部使用假 provider stub 与内存图片，不访问网络、不依赖真实模型。
"""

from __future__ import annotations

import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from astrbot_plugin_alice_image_assistant.alice_image.forward import imagehash
from astrbot_plugin_alice_image_assistant.alice_image.forward.final_verify import (
    FinalVerdict,
    limit_image_edge_sync,
    parse_verdict,
    verify_candidate,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.imagehash import (
    DuplicateFilter,
    ImageFingerprint,
    dhash,
    fingerprint,
    hamming_distance,
    normalize_threshold,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.orchestrator import (
    ForwardSearchOrchestrator,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.query_rewrite import (
    QueryRewriter,
    fallback_query,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.review import ReviewStatus
from astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service import (
    SerpApiForwardService,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.source_selection import (
    analyze_query,
    looks_like_pixiv,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.soutu.service import (
    SoutuSearchService,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.soutu.vlm import (
    parse_selection,
)
from astrbot_plugin_alice_image_assistant.alice_image.forward.vlm_json import (
    coerce_bool,
    coerce_confidence,
    coerce_index,
    coerce_reason,
    parse_json_payload,
)

_FENCE = "\u0060\u0060\u0060"


def _gradient_image(size: int = 256, *, reverse: bool = False) -> Image.Image:
    """构造水平灰度渐变图；reverse=True 时梯度方向相反。"""
    small = Image.new("L", (64, 64))
    data: list[int] = []
    for _ in range(64):
        row = [int(255 * col / 63) for col in range(64)]
        data.extend(list(reversed(row)) if reverse else row)
    small.putdata(data)
    return small.resize((size, size), Image.Resampling.NEAREST).convert("RGB")


def _encode(image: Image.Image, fmt: str = "JPEG", quality: int = 90) -> bytes:
    with io.BytesIO() as buffer:
        image.save(buffer, format=fmt, quality=quality)
        return buffer.getvalue()


def _provider(*texts: str) -> SimpleNamespace:
    """构造只会按序返回给定文本的假 provider。"""
    responses = [
        SimpleNamespace(completion_text=text, result_chain=None) for text in texts
    ]
    return SimpleNamespace(text_chat=AsyncMock(side_effect=responses))


class _Context:
    """SessionReviewResolver 需要的最小 Context 替身。"""

    def __init__(self, provider: object | None = None) -> None:
        self.provider = provider

    def get_provider_by_id(self, _provider_id: str) -> object | None:
        return self.provider

    async def get_current_chat_provider_id(self, _umo: str) -> str:
        return "provider"


class _Event:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send(self, result: object) -> None:
        self.sent.append(result)

    @staticmethod
    def chain_result(chain: object) -> object:
        return chain


class ImageHashTests(unittest.TestCase):
    """C 项：dHash 计算与汉明距离阈值去重。"""

    def test_dhash_length_and_direction(self) -> None:
        forward_hash = dhash(_encode(_gradient_image()))
        reverse_hash = dhash(_encode(_gradient_image(reverse=True)))

        self.assertEqual(len(forward_hash), 16)
        self.assertEqual(len(reverse_hash), 16)
        self.assertEqual(forward_hash, "0" * 16)
        self.assertEqual(reverse_hash, "f" * 16)
        self.assertEqual(hamming_distance(forward_hash, reverse_hash), 64)

    def test_hamming_distance_edge_cases(self) -> None:
        self.assertEqual(hamming_distance("ff00", "ff00"), 0)
        self.assertEqual(hamming_distance("ff00", "ff01"), 1)
        self.assertEqual(hamming_distance("", "ff00"), 64)
        self.assertEqual(hamming_distance("ff", "ff00"), 64)

    def test_hash_of_broken_bytes_is_empty(self) -> None:
        self.assertEqual(dhash(b"not-an-image"), "")
        self.assertEqual(imagehash.average_hash(b""), "")

    def test_fingerprint_rejects_low_resolution(self) -> None:
        item = fingerprint(_encode(_gradient_image(120)), min_resolution=500)

        self.assertFalse(item.valid)
        self.assertEqual((item.width, item.height), (120, 120))

    def test_fingerprint_accepts_large_image(self) -> None:
        item = fingerprint(_encode(_gradient_image(640)), min_resolution=500)

        self.assertTrue(item.valid)
        self.assertEqual(len(item.average_hash), 16)
        self.assertEqual(len(item.difference_hash), 16)

    def test_near_duplicate_is_filtered_by_threshold(self) -> None:
        # dHash 相差 2 位：同一张图的再压缩版本典型特征，应被判为重复。
        first = ImageFingerprint("aaaa", "00ff00ff00ff00ff", 800, 800)
        near = ImageFingerprint("bbbb", "00ff00ff00ff00fc", 800, 800)

        loose = DuplicateFilter(5)
        self.assertTrue(loose.add_if_new(first))
        self.assertTrue(loose.is_duplicate(near))
        self.assertFalse(loose.add_if_new(near))
        self.assertEqual(len(loose), 1)

        strict = DuplicateFilter(1)
        self.assertTrue(strict.add_if_new(first))
        self.assertFalse(strict.is_duplicate(near))
        self.assertTrue(strict.add_if_new(near))

    def test_zero_threshold_keeps_exact_dedup_only(self) -> None:
        exact_only = DuplicateFilter(0)
        first = ImageFingerprint("aaaa", "00ff00ff00ff00ff", 800, 800)
        near = ImageFingerprint("bbbb", "00ff00ff00ff00fc", 800, 800)
        same = ImageFingerprint("aaaa", "1234123412341234", 800, 800)

        self.assertTrue(exact_only.add_if_new(first))
        self.assertTrue(exact_only.add_if_new(near))
        self.assertTrue(exact_only.is_duplicate(same))

    def test_recompressed_image_is_treated_as_duplicate(self) -> None:
        original = _gradient_image(640)
        dedup = DuplicateFilter(5)

        self.assertTrue(dedup.add_if_new(fingerprint(_encode(original, quality=95))))
        recompressed = fingerprint(
            _encode(original.resize((520, 520), Image.Resampling.LANCZOS), quality=25)
        )
        self.assertTrue(dedup.is_duplicate(recompressed))

    def test_invalid_fingerprint_is_never_duplicate(self) -> None:
        dedup = DuplicateFilter(5)

        self.assertFalse(dedup.is_duplicate(ImageFingerprint()))
        self.assertFalse(dedup.add_if_new(ImageFingerprint()))

    def test_normalize_threshold_bounds(self) -> None:
        self.assertEqual(normalize_threshold("abc"), 5)
        self.assertEqual(normalize_threshold(None), 5)
        self.assertEqual(normalize_threshold(99), 16)
        self.assertEqual(normalize_threshold(-3), 0)
        self.assertEqual(normalize_threshold(4), 4)


class SourceSelectionTests(unittest.TestCase):
    """F 项：来源判定表驱动纯函数，反向信号优先于正向信号。"""

    def test_pixiv_and_real_image_cases(self) -> None:
        cases = [
            ("初音ミク 插画", True),
            ("动漫壁纸 少女", True),
            ("角色立绘 白色连衣裙", True),
            ("pixiv 排行榜 同人", True),
            ("anime illustration girl", True),
            ("朝日奈みらい", True),
            ("真人 cos 动漫角色", False),
            ("壁纸照片 城市夜景", False),
            ("海狸 真实照片", False),
            ("东京街头 摄影作品", False),
            ("cosplay 初音未来", False),
            ("雪山日出", False),
            ("游戏 截图", False),
            ("cosmos 星空 插画", True),
        ]
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(looks_like_pixiv(query), expected)

    def test_negative_keyword_overrides_positive(self) -> None:
        analysis = analyze_query("真人 cos 动漫角色")

        self.assertFalse(analysis.prefer_pixiv)
        self.assertIn("真人", analysis.negative_hits)
        self.assertIn("动漫", analysis.positive_hits)
        self.assertIn("反向关键词", analysis.reason)

    def test_ascii_keyword_uses_word_boundary(self) -> None:
        analysis = analyze_query("cosmos 星空 插画")

        self.assertEqual(analysis.negative_hits, [])
        self.assertTrue(analysis.prefer_pixiv)

    def test_kana_only_query_prefers_pixiv(self) -> None:
        analysis = analyze_query("ホロライブ")

        self.assertTrue(analysis.prefer_pixiv)
        self.assertTrue(analysis.kana)

    def test_empty_query_defaults_to_real_image(self) -> None:
        analysis = analyze_query("")

        self.assertFalse(analysis.prefer_pixiv)
        self.assertIn("默认走真实图片源", analysis.reason)


class VlmJsonTests(unittest.TestCase):
    """B 项：置信度 / 理由解析与旧格式兼容。"""

    def test_coerce_confidence_variants(self) -> None:
        cases = [
            (0.86, 0.86),
            ("0.86", 0.86),
            (86, 0.86),
            ("86%", 0.86),
            (120, 1.0),
            (-1, 0.0),
            ("abc", None),
            ("", None),
            (None, None),
            (True, None),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                actual = coerce_confidence(raw)
                if expected is None:
                    self.assertIsNone(actual)
                else:
                    self.assertAlmostEqual(actual, expected, places=4)

    def test_parse_json_payload_penetrates_fence(self) -> None:
        text = _FENCE + "json\n" + '{"best_index": 2, "confidence": 0.9}' + "\n" + _FENCE
        data = parse_json_payload(text, required_key="best_index")

        self.assertIsNotNone(data)
        self.assertEqual(data["best_index"], 2)

    def test_parse_json_payload_takes_last_object(self) -> None:
        text = '先想一下 {"best_index": 1} 再修正 {"best_index": 3}'

        self.assertEqual(parse_json_payload(text)["best_index"], 3)

    def test_parse_json_payload_requires_key(self) -> None:
        self.assertIsNone(parse_json_payload('{"other": 1}', required_key="best_index"))
        self.assertIsNone(parse_json_payload("完全不是 JSON"))

    def test_coerce_bool_and_index_and_reason(self) -> None:
        self.assertTrue(coerce_bool("符合"))
        self.assertFalse(coerce_bool("不匹配"))
        self.assertIsNone(coerce_bool("说不清"))
        self.assertEqual(coerce_index("第 3 张"), 3)
        self.assertIsNone(coerce_index("无"))
        self.assertEqual(coerce_reason("  银发\n白裙  "), "银发 白裙")
        self.assertEqual(len(coerce_reason("啊" * 500)), 200)


class CollageSelectionParseTests(unittest.TestCase):
    """B 项：拼图选图解析同时支持新旧格式。"""

    def test_new_format_with_confidence_and_reason(self) -> None:
        selection = parse_selection(
            '{"best_index": 3, "confidence": 0.78, "reason": "银发少女白裙"}', 9
        )

        self.assertEqual(selection.index, 2)
        self.assertAlmostEqual(selection.confidence, 0.78, places=4)
        self.assertEqual(selection.reason, "银发少女白裙")
        self.assertTrue(selection.matched)

    def test_legacy_format_without_confidence(self) -> None:
        selection = parse_selection('{"best_index": 2}', 9)

        self.assertEqual(selection.index, 1)
        self.assertIsNone(selection.confidence)
        self.assertEqual(selection.reason, "")

    def test_zero_means_no_match(self) -> None:
        self.assertEqual(parse_selection('{"best_index": 0}', 9).index, -1)
        self.assertEqual(parse_selection("0", 9).index, -1)

    def test_bare_number_and_fenced_payload(self) -> None:
        self.assertEqual(parse_selection("4", 9).index, 3)
        fenced = _FENCE + "json\n" + '{"best_index": 5, "confidence": 0.5}' + "\n" + _FENCE
        self.assertEqual(parse_selection(fenced, 9).index, 4)

    def test_regex_fallback_for_truncated_json(self) -> None:
        selection = parse_selection('{"best_index": 6, "confi', 9)

        self.assertEqual(selection.index, 5)

    def test_unparseable_response_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_selection("我不确定", 9)


class QueryRewriteTests(unittest.IsolatedAsyncioTestCase):
    """D 项：查询改写、按源变体、缓存与失败回退。"""

    _PAYLOAD = (
        '{"zh": "初音未来 白色连衣裙 插画",'
        ' "en": "hatsune miku white dress illustration",'
        ' "ja_tags": ["初音ミク", "白ワンピース"],'
        ' "negative": ["cosplay"]}'
    )

    async def test_successful_rewrite_produces_per_source_variants(self) -> None:
        rewriter = QueryRewriter({})
        provider = _provider(self._PAYLOAD)

        result = await rewriter.rewrite(
            provider, "帮我找一张可爱的初音未来穿白色连衣裙的图"
        )

        self.assertTrue(result.rewritten)
        self.assertEqual(result.for_source("soutu"), "初音未来 白色连衣裙 插画")
        self.assertEqual(
            result.for_source("serpapi"), "hatsune miku white dress illustration"
        )
        self.assertEqual(result.for_source("pixiv"), "初音ミク 白ワンピース")
        self.assertEqual(result.negative, ["cosplay"])
        self.assertEqual(
            result.to_json_dict()["original"],
            "帮我找一张可爱的初音未来穿白色连衣裙的图",
        )

    async def test_cache_avoids_duplicate_provider_calls(self) -> None:
        rewriter = QueryRewriter({"cache_size": 8})
        provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text=self._PAYLOAD, result_chain=None
                )
            )
        )

        first = await rewriter.rewrite(provider, "初音未来 白裙")
        second = await rewriter.rewrite(provider, "初音未来 白裙")

        self.assertEqual(provider.text_chat.await_count, 1)
        self.assertEqual(first.zh, second.zh)

    async def test_zero_cache_size_disables_cache(self) -> None:
        rewriter = QueryRewriter({"cache_size": 0})
        provider = SimpleNamespace(
            text_chat=AsyncMock(
                return_value=SimpleNamespace(
                    completion_text=self._PAYLOAD, result_chain=None
                )
            )
        )

        await rewriter.rewrite(provider, "初音未来 白裙")
        await rewriter.rewrite(provider, "初音未来 白裙")

        self.assertEqual(provider.text_chat.await_count, 2)

    async def test_provider_failure_falls_back_to_original_query(self) -> None:
        rewriter = QueryRewriter({})
        provider = SimpleNamespace(text_chat=AsyncMock(side_effect=RuntimeError("boom")))

        result = await rewriter.rewrite(provider, "雪山日出")

        self.assertFalse(result.rewritten)
        self.assertEqual(result.for_source("soutu"), "雪山日出")
        self.assertEqual(result.for_source("pixiv"), "雪山日出")
        self.assertEqual(result.for_source("serpapi"), "雪山日出")
        self.assertIn("boom", result.error)

    async def test_unparseable_response_falls_back(self) -> None:
        rewriter = QueryRewriter({})

        result = await rewriter.rewrite(_provider("我想不出来"), "雪山日出")

        self.assertFalse(result.rewritten)
        self.assertEqual(result.zh, "雪山日出")

    async def test_missing_provider_and_disabled_switch_fall_back(self) -> None:
        enabled = QueryRewriter({})
        disabled = QueryRewriter({"enabled": False})
        provider = _provider(self._PAYLOAD)

        no_provider = await enabled.rewrite(None, "雪山日出")
        switched_off = await disabled.rewrite(provider, "雪山日出")

        self.assertFalse(no_provider.rewritten)
        self.assertFalse(switched_off.rewritten)
        provider.text_chat.assert_not_awaited()

    async def test_fallback_query_helper_keeps_original(self) -> None:
        result = fallback_query("初音ミク", "无可用模型")

        self.assertFalse(result.rewritten)
        self.assertEqual(result.original, "初音ミク")
        self.assertEqual(result.for_source("unknown"), "初音ミク")
        self.assertEqual(result.error, "无可用模型")


class FinalVerifyTests(unittest.IsolatedAsyncioTestCase):
    """A 项：单图全分辨率复核的判定与失败语义。"""

    async def test_accepts_high_confidence_match(self) -> None:
        provider = _provider(
            '{"match": true, "confidence": 0.86, "reason": "银发、白裙、草地均符合"}'
        )

        verdict = await verify_candidate(
            provider, _encode(_gradient_image(320)), "银发少女白裙草地", retries=0
        )

        self.assertTrue(verdict.match)
        self.assertFalse(verdict.errored)
        self.assertAlmostEqual(verdict.confidence, 0.86, places=4)
        self.assertTrue(verdict.accepted(0.6))
        self.assertIn("银发", verdict.reason)

    async def test_rejects_low_confidence_match(self) -> None:
        provider = _provider('{"match": true, "confidence": 0.35, "reason": "看不清"}')

        verdict = await verify_candidate(
            provider, _encode(_gradient_image(320)), "银发少女", retries=0
        )

        self.assertTrue(verdict.match)
        self.assertFalse(verdict.errored)
        self.assertFalse(verdict.accepted(0.6))

    async def test_rejects_explicit_mismatch(self) -> None:
        provider = _provider('{"match": false, "confidence": 0.95, "reason": "黑发"}')

        verdict = await verify_candidate(
            provider, _encode(_gradient_image(320)), "银发少女", retries=0
        )

        self.assertFalse(verdict.accepted(0.6))
        self.assertFalse(verdict.errored)

    async def test_unparseable_response_is_a_technical_error(self) -> None:
        provider = _provider("我看不出来")

        verdict = await verify_candidate(
            provider, _encode(_gradient_image(320)), "银发少女", retries=0
        )

        self.assertTrue(verdict.errored)
        self.assertFalse(verdict.accepted(0.0))

    async def test_missing_provider_or_empty_image_is_an_error(self) -> None:
        no_provider = await verify_candidate(None, b"x", "银发少女", retries=0)
        no_image = await verify_candidate(_provider("{}"), b"", "银发少女", retries=0)

        self.assertTrue(no_provider.errored)
        self.assertTrue(no_image.errored)

    async def test_confidence_only_response_is_treated_as_match(self) -> None:
        verdict = parse_verdict('{"confidence": 0.9, "reason": "看起来符合"}')

        self.assertIsNotNone(verdict)
        self.assertTrue(verdict.match)
        self.assertTrue(verdict.accepted(0.6))

    def test_parse_verdict_returns_none_for_garbage(self) -> None:
        self.assertIsNone(parse_verdict("没有 JSON"))
        self.assertIsNone(parse_verdict('{"reason": "只有理由"}'))

    def test_missing_confidence_only_checks_match_flag(self) -> None:
        self.assertTrue(FinalVerdict(match=True).accepted(0.9))
        self.assertFalse(FinalVerdict(match=False).accepted(0.0))
        self.assertFalse(FinalVerdict(match=True, error="boom").accepted(0.0))

    def test_limit_image_edge_shrinks_only_when_needed(self) -> None:
        large = _encode(_gradient_image(2000))
        small = _encode(_gradient_image(320))

        shrunk = limit_image_edge_sync(large, 1280)
        with Image.open(io.BytesIO(shrunk)) as image:
            self.assertLessEqual(max(image.width, image.height), 1280)
        self.assertIs(limit_image_edge_sync(small, 1280), small)
        self.assertIs(limit_image_edge_sync(b"broken", 1280), b"broken")


_SOUTU_ITEMS: tuple[tuple[str, bytes], ...] = (
    ("u1", b"one"),
    ("u2", b"two"),
    ("u3", b"three"),
)


class SoutuFinalVerifyTests(unittest.IsolatedAsyncioTestCase):
    """A 项：soutu 拼图候选的单图全分辨率终选复核；含缺陷 4（并发上限接线）。"""

    VERIFY_TARGET = (
        "astrbot_plugin_alice_image_assistant.alice_image.forward.soutu"
        ".service.verify_candidate"
    )

    def _service(self, **review: object) -> SoutuSearchService:
        service = SoutuSearchService(_Context(), {})
        service.configure_review(dict(review))
        return service

    async def test_final_verify_pass_returns_matched(self) -> None:
        service = self._service()
        service._select_on_collage = AsyncMock(
            return_value=(None, list(_SOUTU_ITEMS), 0, 0.8, "拼图理由")
        )
        verdict = FinalVerdict(match=True, confidence=0.91, reason="银发白裙草地均符合")

        with patch(self.VERIFY_TARGET, AsyncMock(return_value=verdict)) as verify:
            selection = await service._vlm_selection(
                object(), list(_SOUTU_ITEMS), "银发白裙少女"
            )

        verify.assert_awaited_once()
        self.assertIs(selection.status, ReviewStatus.MATCHED)
        self.assertEqual(selection.image_url, "u1")
        self.assertEqual(selection.confidence, 0.91)
        self.assertEqual(selection.reason, "银发白裙草地均符合")

    async def test_rejected_champion_falls_back_to_remaining_candidate(self) -> None:
        service = self._service()
        remaining = [("u2", b"two"), ("u3", b"three")]
        service._select_on_collage = AsyncMock(
            side_effect=[
                (None, list(_SOUTU_ITEMS), 0, 0.8, "第一轮"),
                (None, remaining, 0, 0.75, "第二轮"),
            ]
        )
        verdicts = [
            FinalVerdict(match=False, confidence=0.2, reason="发色不符"),
            FinalVerdict(match=True, confidence=0.88, reason="全部要素符合"),
        ]

        with patch(self.VERIFY_TARGET, AsyncMock(side_effect=verdicts)) as verify:
            selection = await service._vlm_selection(
                object(), list(_SOUTU_ITEMS), "银发白裙少女"
            )

        self.assertEqual(verify.await_count, 2)
        self.assertIs(selection.status, ReviewStatus.MATCHED)
        self.assertEqual(selection.image_url, "u2")
        self.assertEqual(selection.confidence, 0.88)

    async def test_verify_error_keeps_candidate_for_fail_open(self) -> None:
        service = self._service()
        service._select_on_collage = AsyncMock(
            return_value=(None, list(_SOUTU_ITEMS), 0, 0.8, "拼图理由")
        )
        verdict = FinalVerdict(match=False, error="provider 不可用")

        with patch(self.VERIFY_TARGET, AsyncMock(return_value=verdict)):
            selection = await service._vlm_selection(
                object(), list(_SOUTU_ITEMS), "银发白裙少女"
            )

        self.assertIs(selection.status, ReviewStatus.ERROR)
        self.assertEqual(selection.image_bytes, b"one")
        self.assertIn("终选复核失败", selection.error)

    async def test_low_collage_confidence_skips_verify_and_gives_no_match(self) -> None:
        service = self._service(final_verify_retry_limit=0)
        service._select_on_collage = AsyncMock(
            return_value=(None, list(_SOUTU_ITEMS), 0, 0.3, "不太确定")
        )

        with patch(self.VERIFY_TARGET, AsyncMock()) as verify:
            selection = await service._vlm_selection(
                object(), list(_SOUTU_ITEMS), "银发白裙少女"
            )

        verify.assert_not_awaited()
        self.assertIs(selection.status, ReviewStatus.NO_MATCH)
        self.assertEqual(selection.confidence, 0.3)

    async def test_final_verify_switch_off_returns_collage_choice(self) -> None:
        service = self._service(final_verify_enabled=False)
        service._select_on_collage = AsyncMock(
            return_value=(None, list(_SOUTU_ITEMS), 1, 0.7, "拼图理由")
        )

        with patch(self.VERIFY_TARGET, AsyncMock()) as verify:
            selection = await service._vlm_selection(
                object(), list(_SOUTU_ITEMS), "银发白裙少女"
            )

        verify.assert_not_awaited()
        self.assertIs(selection.status, ReviewStatus.MATCHED)
        self.assertEqual(selection.image_url, "u2")
        self.assertEqual(selection.confidence, 0.7)

    def test_vlm_concurrency_follows_review_config(self) -> None:
        """缺陷 4：Semaphore(2) 硬编码改为读 llm_review.max_concurrency。"""
        default_service = self._service()
        tuned = self._service(max_concurrency=5)
        clamped = self._service(max_concurrency=99)
        broken = self._service(max_concurrency="x")

        self.assertEqual(default_service._semaphore()._value, 2)
        self.assertEqual(tuned._semaphore()._value, 5)
        self.assertEqual(clamped._semaphore()._value, 8)
        self.assertEqual(broken._semaphore()._value, 2)

    async def test_download_batch_drops_near_duplicates(self) -> None:
        """C 项接线：同图不同压缩率只占用一个视觉模型评估名额。"""
        service = self._service()
        high = _encode(_gradient_image(600), quality=95)
        low = _encode(_gradient_image(600), quality=25)
        service.composer_mgr.download_image_batch = AsyncMock(
            return_value=[("a", high), ("b", low)]
        )

        items = await service._download_valid_batch(
            ["a", "b"], 2, 0, DuplicateFilter(5)
        )

        self.assertEqual([url for url, _ in items], ["a"])


class SerpFinalVerifyTests(unittest.IsolatedAsyncioTestCase):
    """A 项：SerpApi 淘汰赛决赛圈接入终选复核。"""

    MODULE = "astrbot_plugin_alice_image_assistant.alice_image.forward.serpapi.service"

    def _service(self, **review: object) -> SerpApiForwardService:
        service = SerpApiForwardService(_Context(), {"serpapi_keys": ["key"]})
        service.configure_review(dict(review))
        self.addAsyncCleanup(service.close)
        return service

    async def test_winner_accepted_by_final_verify(self) -> None:
        service = self._service()
        verdict = FinalVerdict(match=True, confidence=0.9, reason="要素全中")

        with (
            patch(self.MODULE + ".download_image", AsyncMock(return_value=b"img")),
            patch(self.MODULE + ".verify_candidate", AsyncMock(return_value=verdict)),
        ):
            url, status, result = await service._final_verify_finalists(
                object(), "w", ["w", "second"], "雪山日出"
            )

        self.assertEqual(url, "w")
        self.assertIs(status, ReviewStatus.MATCHED)
        self.assertEqual(result.confidence, 0.9)

    async def test_rejected_winner_switches_to_runner_up(self) -> None:
        service = self._service()
        verdicts = [
            FinalVerdict(match=False, confidence=0.1, reason="不是雪山"),
            FinalVerdict(match=True, confidence=0.82, reason="雪山日出符合"),
        ]

        with (
            patch(self.MODULE + ".download_image", AsyncMock(return_value=b"img")),
            patch(self.MODULE + ".verify_candidate", AsyncMock(side_effect=verdicts)),
        ):
            url, status, result = await service._final_verify_finalists(
                object(), "w", ["w", "second"], "雪山日出"
            )

        self.assertEqual(url, "second")
        self.assertIs(status, ReviewStatus.MATCHED)
        self.assertEqual(result.confidence, 0.82)

    async def test_verify_error_keeps_tournament_winner(self) -> None:
        service = self._service()
        verdict = FinalVerdict(match=False, error="解析失败")

        with (
            patch(self.MODULE + ".download_image", AsyncMock(return_value=b"img")),
            patch(self.MODULE + ".verify_candidate", AsyncMock(return_value=verdict)),
        ):
            url, status, _result = await service._final_verify_finalists(
                object(), "w", ["w", "second"], "雪山日出"
            )

        self.assertEqual(url, "w")
        self.assertIs(status, ReviewStatus.ERROR)

    async def test_all_finalists_rejected_reports_no_match(self) -> None:
        service = self._service()
        rejections = [
            FinalVerdict(match=False, confidence=0.1),
            FinalVerdict(match=False, confidence=0.2),
            FinalVerdict(match=False, confidence=0.3),
        ]

        with (
            patch(self.MODULE + ".download_image", AsyncMock(return_value=b"img")),
            patch(self.MODULE + ".verify_candidate", AsyncMock(side_effect=rejections)),
        ):
            url, status, result = await service._final_verify_finalists(
                object(), "w", ["w", "second", "third"], "雪山日出"
            )

        self.assertEqual(url, "w")
        self.assertIs(status, ReviewStatus.NO_MATCH)
        self.assertEqual(result.confidence, 0.3)

    async def test_undownloadable_finalist_is_skipped(self) -> None:
        service = self._service()
        verdict = FinalVerdict(match=True, confidence=0.77)

        with (
            patch(
                self.MODULE + ".download_image", AsyncMock(side_effect=[None, b"img"])
            ),
            patch(self.MODULE + ".verify_candidate", AsyncMock(return_value=verdict)),
        ):
            url, status, _result = await service._final_verify_finalists(
                object(), "w", ["w", "second"], "雪山日出"
            )

        self.assertEqual(url, "second")
        self.assertIs(status, ReviewStatus.MATCHED)

    async def test_search_uses_runner_up_when_champion_rejected(self) -> None:
        service = self._service()
        service._get_vlm_provider = AsyncMock(return_value=object())

        async def _tournament(*_args: object, **kwargs: object) -> str:
            kwargs["finalists_out"][:] = ["w", "second"]
            return "w"

        verdicts = [
            FinalVerdict(match=False, confidence=0.1, reason="不是雪山"),
            FinalVerdict(match=True, confidence=0.9, reason="雪山日出符合"),
        ]
        with (
            patch(
                self.MODULE + ".fetch_image_urls",
                AsyncMock(return_value=["w", "second", "third"]),
            ),
            patch(self.MODULE + ".run_tournament", _tournament),
            patch(self.MODULE + ".download_image", AsyncMock(return_value=b"img")),
            patch(self.MODULE + ".verify_candidate", AsyncMock(side_effect=verdicts)),
        ):
            result = await service.search(_Event(), "snow mountain", "雪山日出")

        self.assertEqual(result.image_url, "second")
        self.assertIs(result.review_status, ReviewStatus.MATCHED)
        self.assertEqual(result.review_confidence, 0.9)
        self.assertEqual(result.review_reason, "雪山日出符合")
        self.assertEqual(result.image_bytes, b"img")


class _ByteStub:
    """soutu / serpapi 的最小替身，记录收到的检索词并回放指定审核结果。"""

    def __init__(
        self,
        *,
        image_bytes: bytes | None = b"image",
        error: str = "",
        review_status: ReviewStatus = ReviewStatus.MATCHED,
        review_confidence: float | None = None,
        review_reason: str = "",
        review_fallback: bool = False,
        resolver: object | None = None,
    ) -> None:
        self.image_bytes = image_bytes
        self.error = error
        self.review_status = review_status
        self.review_confidence = review_confidence
        self.review_reason = review_reason
        self.review_fallback = review_fallback
        self.calls = 0
        self.queries: list[str] = []
        self.descriptions: list[str] = []
        self.review_config: dict[str, object] | None = None
        if resolver is not None:
            self.review_resolver = resolver

    def available(self) -> bool:
        return True

    def configure_review(self, review_config: dict[str, object] | None) -> None:
        self.review_config = review_config

    async def search(
        self,
        _event: object,
        search_query: str = "",
        description: str = "",
        **_kwargs: object,
    ) -> SimpleNamespace:
        self.calls += 1
        self.queries.append(search_query)
        self.descriptions.append(description)
        return SimpleNamespace(
            image_bytes=self.image_bytes,
            error=self.error,
            review_fallback=self.review_fallback,
            review_status=self.review_status,
            review_confidence=self.review_confidence,
            review_reason=self.review_reason,
        )

    async def close(self) -> None:
        return None

    async def terminate(self) -> None:
        return None


def _forward_config(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "fallback_enabled": True,
        "fallback_order": ["pixiv", "soutu", "serpapi"],
        "auto_source_enabled": True,
        "tool_send_images": False,
        "pixiv": {"enabled": False},
        "soutu": {"enabled": True, "vlm_selection_enabled": True},
        "serpapi": {"enabled": True},
        "llm_review": {"enabled": True, "commands_enabled": True},
        "query_rewrite": {"enabled": False},
    }
    value.update(overrides)
    return value


class OrchestratorPrecisionTests(unittest.IsolatedAsyncioTestCase):
    """编排层：缺陷 1/2 回归 + D 项检索词接线 + E 项跨源择优 + 结构化透传。"""

    async def test_out_of_range_count_reports_warning(self) -> None:
        """缺陷 1：越界数量不再静默裁剪，写入 warnings 供调用方感知。"""
        soutu = _ByteStub()
        service = ForwardSearchOrchestrator(_forward_config(), None, soutu, _ByteStub())

        result = await service.search(_Event(), "雪山", "雪山日出", "soutu", count=10)

        self.assertTrue(result.success)
        self.assertIn("count", result.warnings)
        self.assertIn("超出支持范围", result.warnings["count"])

    async def test_unparseable_count_reports_warning(self) -> None:
        service = ForwardSearchOrchestrator(
            _forward_config(), None, _ByteStub(), _ByteStub()
        )

        result = await service.search(_Event(), "雪山", "雪山日出", "soutu", count="abc")

        self.assertIn("count", result.warnings)
        self.assertIn("无法解析", result.warnings["count"])

    async def test_in_range_count_has_no_warning(self) -> None:
        service = ForwardSearchOrchestrator(
            _forward_config(), None, _ByteStub(), _ByteStub()
        )

        result = await service.search(_Event(), "雪山", "雪山日出", "soutu", count=3)

        self.assertNotIn("count", result.warnings)

    async def test_no_match_follows_strict_match_and_switches_source(self) -> None:
        """缺陷 2：-1（全不匹配）走 strict_match_enabled 语义。"""
        soutu = _ByteStub(image_bytes=None, review_status=ReviewStatus.NO_MATCH)
        serp = _ByteStub(review_confidence=0.8)
        config = _forward_config(
            llm_review={
                "enabled": True,
                "commands_enabled": True,
                "strict_match_enabled": True,
            }
        )
        service = ForwardSearchOrchestrator(config, None, soutu, serp)

        result = await service.search(_Event(), "雪山", "雪山日出", "soutu")

        self.assertTrue(result.success)
        self.assertEqual(result.source, "serpapi")
        self.assertIn("不匹配", result.errors["soutu"])

    async def test_review_error_with_fail_open_releases_candidate(self) -> None:
        """缺陷 2：-2（审核链路出错）走 review_fail_open 语义，默认放行首图。"""
        soutu = _ByteStub(review_status=ReviewStatus.ERROR, review_fallback=True)
        serp = _ByteStub()
        service = ForwardSearchOrchestrator(_forward_config(), None, soutu, serp)

        result = await service.search(_Event(), "雪山", "雪山日出", "soutu")

        self.assertTrue(result.success)
        self.assertEqual(result.source, "soutu")
        self.assertTrue(result.review_fallback)
        self.assertEqual(serp.calls, 0)

    async def test_review_error_without_fail_open_switches_source(self) -> None:
        soutu = _ByteStub(review_status=ReviewStatus.ERROR)
        serp = _ByteStub()
        config = _forward_config(
            llm_review={
                "enabled": True,
                "commands_enabled": True,
                "fail_open": False,
            }
        )
        service = ForwardSearchOrchestrator(config, None, soutu, serp)

        result = await service.search(_Event(), "雪山", "雪山日出", "soutu")

        self.assertTrue(result.success)
        self.assertEqual(result.source, "serpapi")
        self.assertIn("不放行首图", result.errors["soutu"])

    async def test_best_of_picks_highest_confidence_source(self) -> None:
        """E 项：开启跨源择优后按置信度选源，而不是先返回者优先。"""
        soutu = _ByteStub(review_confidence=0.62, review_reason="大致相符")
        serp = _ByteStub(review_confidence=0.91, review_reason="要素全中")
        service = ForwardSearchOrchestrator(
            _forward_config(best_of_enabled=True), None, soutu, serp
        )

        result = await service.search(_Event(), "雪山", "雪山日出", "auto")

        self.assertTrue(result.success)
        self.assertEqual(result.source, "serpapi")
        self.assertEqual(result.review_confidence, 0.91)
        self.assertEqual(result.review_reason, "要素全中")
        self.assertEqual(result.source_scores["soutu"], 0.62)
        self.assertEqual(result.source_scores["serpapi"], 0.91)
        self.assertEqual(soutu.calls, 1)
        self.assertEqual(serp.calls, 1)

    async def test_best_of_disabled_by_default_keeps_first_success(self) -> None:
        soutu = _ByteStub(review_confidence=0.62)
        serp = _ByteStub(review_confidence=0.91)
        service = ForwardSearchOrchestrator(_forward_config(), None, soutu, serp)

        result = await service.search(_Event(), "雪山", "雪山日出", "auto")

        self.assertEqual(result.source, "soutu")
        self.assertEqual(serp.calls, 0)

    async def test_best_of_falls_through_when_no_source_usable(self) -> None:
        soutu = _ByteStub(image_bytes=None, error="soutu failed")
        serp = _ByteStub(image_bytes=None, error="serp failed")
        service = ForwardSearchOrchestrator(
            _forward_config(best_of_enabled=True), None, soutu, serp
        )

        result = await service.search(_Event(), "雪山", "雪山日出", "auto")

        self.assertFalse(result.success)
        self.assertEqual(set(result.errors), {"soutu", "serpapi"})

    async def test_query_rewrite_feeds_source_specific_query(self) -> None:
        """D 项：soutu 收到精炼中文检索式，匹配判定仍用原始描述。"""
        payload = (
            '{"zh": "雪山 日出 航拍", "en": "snow mountain sunrise aerial", '
            '"ja_tags": ["雪山", "朝日"], "negative": ["插画"]}'
        )
        resolver = SimpleNamespace(resolve=AsyncMock(return_value=_provider(payload)))
        soutu = _ByteStub(review_confidence=0.7, resolver=resolver)
        config = _forward_config(query_rewrite={"enabled": True, "cache_size": 8})
        service = ForwardSearchOrchestrator(config, None, soutu, _ByteStub())

        result = await service.search(
            _Event(), "帮我找一张可爱的雪山日出照片", "雪山日出", "soutu"
        )

        self.assertTrue(result.success)
        self.assertEqual(soutu.queries[0], "雪山 日出 航拍")
        self.assertEqual(soutu.descriptions[0], "雪山日出")
        self.assertTrue(result.query_rewrite["rewritten"])
        self.assertEqual(result.query_rewrite["ja_tags"], ["雪山", "朝日"])

    async def test_query_rewrite_failure_falls_back_to_original_query(self) -> None:
        resolver = SimpleNamespace(resolve=AsyncMock(side_effect=RuntimeError("boom")))
        soutu = _ByteStub(resolver=resolver)
        config = _forward_config(query_rewrite={"enabled": True})
        service = ForwardSearchOrchestrator(config, None, soutu, _ByteStub())

        result = await service.search(_Event(), "雪山日出", "雪山日出", "soutu")

        self.assertTrue(result.success)
        self.assertEqual(soutu.queries[0], "雪山日出")
        self.assertFalse(result.query_rewrite["rewritten"])

    async def test_review_config_is_pushed_to_sources(self) -> None:
        soutu, serp = _ByteStub(), _ByteStub()
        config = _forward_config(
            llm_review={
                "enabled": True,
                "commands_enabled": True,
                "final_verify_enabled": True,
                "confidence_threshold": 0.7,
                "max_concurrency": 4,
            }
        )
        service = ForwardSearchOrchestrator(config, None, soutu, serp)

        await service.search(_Event(), "雪山", "雪山日出", "soutu")

        self.assertEqual(soutu.review_config["confidence_threshold"], 0.7)
        self.assertEqual(serp.review_config["max_concurrency"], 4)

    async def test_outcome_json_exposes_decision_fields(self) -> None:
        """B 项：置信度 / 理由 / 各源评分 / 改写结果都要能透出给 LLM 与 WebUI。"""
        soutu = _ByteStub(review_confidence=0.83, review_reason="银发白裙均符合")
        service = ForwardSearchOrchestrator(
            _forward_config(), None, soutu, _ByteStub()
        )

        result = await service.search(_Event(), "雪山", "雪山日出", "soutu")
        payload = json.loads(result.to_json())

        self.assertEqual(payload["review_confidence"], 0.83)
        self.assertEqual(payload["review_reason"], "银发白裙均符合")
        self.assertEqual(payload["source_scores"], {"soutu": 0.83})
        self.assertEqual(payload["query_rewrite"]["original"], "雪山")
        self.assertFalse(payload["query_rewrite"]["rewritten"])


if __name__ == "__main__":
    unittest.main()
