from __future__ import annotations

import asyncio
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

from astrbot_plugin_alice_image_assistant.alice_image.reverse import controller as ctl
from astrbot_plugin_alice_image_assistant.alice_image.reverse import evidence as ev
from astrbot_plugin_alice_image_assistant.alice_image.reverse.controller import (
    AliceReverseController,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.image_context import (
    ImageContextManager,
    SessionImages,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.models import (
    SearchResultItem,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.ranking import merge_and_rank
from astrbot_plugin_alice_image_assistant.alice_image.reverse.service import (
    AliceImageReverseService,
)
from astrbot_plugin_alice_image_assistant.alice_image.tools import AliceReverseImageTool


def item(title: str, source: str = "Google Lens", score: float = 0.5) -> SearchResultItem:
    return SearchResultItem(
        title=title,
        url=f"https://example.com/{title}",
        source=source,
        score=score,
        thumbnail="https://cdn.example.com/a.jpg",
        description="页面摘要",
        source_key="google_lens",
    )


class Strategy:
    def __init__(self, name: str, items: list[SearchResultItem]):
        self.name, self.items = name, items
        self.search = AsyncMock(return_value=items)
        self.close = AsyncMock()

    def get_service_name(self):
        return self.name


class ReverseToolBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = AliceReverseController(
            Mock(),
            {
                "strategies": {
                    f"enable_{name}": False
                    for name in ("saucenao", "google_lens", "ascii2d", "yandex")
                },
                "ai_behavior": {"visual_evidence_enabled": False},
            },
        )
        self.strategy = Strategy("Google Lens", [item("forum"), item("specific_source")])
        self.controller.strategies = [self.strategy]
        self.controller.service = AliceImageReverseService([self.strategy], max_results=1)
        self.controller._send_search_results = AsyncMock()
        self.controller.service._fill_thumbnails = AsyncMock()
        self.event = SimpleNamespace(
            unified_msg_origin="bot:group:1",
            session_id="1",
            message_obj=SimpleNamespace(message_id="m1"),
            send=AsyncMock(),
            plain_result=lambda x: x,
        )
        self.manager = ctl.get_image_context_manager()
        self.manager.add_image(self.event, "https://example.com/target.jpg")
        self.image_id = self.manager.get_image_context_info(self.event)["images"][0]["image_id"]

    async def asyncTearDown(self):
        await self.controller.terminate()

    async def search(self, **kwargs):
        return json.loads(
            await self.controller.tool_search_image(self.event, image_id=self.image_id, **kwargs)
        )

    async def test_defaults_silent_even_with_old_false_and_returns_more_than_display_limit(self):
        self.controller.config["ai_behavior"]["llm_tool_silent_mode"] = False
        result = await self.search()
        self.assertTrue(result["success"])
        self.assertFalse(result["message_sent"])
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][1]["description"], "页面摘要")
        self.assertEqual(result["items"][1]["thumbnail_url"], "https://cdn.example.com/a.jpg")
        self.assertIn("not_probability", result["items"][0]["score_kind"])
        self.assertNotIn("请向用户展示", result["instruction"])
        self.event.send.assert_not_awaited()
        self.controller._send_search_results.assert_not_awaited()
        self.controller.service._fill_thumbnails.assert_not_awaited()

    async def test_config_default_and_explicit_false_override(self):
        self.controller.config["ai_behavior"]["llm_tool_send_results_default"] = True
        silent = await self.search(send_results=False)
        self.assertFalse(silent["message_sent"])
        result = await self.search()
        self.assertTrue(result["message_sent"])
        self.controller._send_search_results.assert_awaited_once()
        self.assertEqual(len(self.controller._send_search_results.call_args.args[1]), 1)

    async def test_repeat_same_turn_reuses_search_and_sends_at_most_once(self):
        results = await asyncio.gather(
            self.search(send_results=True), self.search(send_results=True)
        )
        self.assertTrue(all(result["message_sent"] for result in results))
        self.assertEqual(sum(result["cache_hit"] for result in results), 1)
        self.strategy.search.assert_awaited_once()
        self.controller._send_search_results.assert_awaited_once()
        self.event.message_obj.message_id = "m2"
        await self.search()
        self.assertEqual(self.strategy.search.await_count, 2)

    async def test_invalid_boolean_string_never_sends(self):
        result = await self.search(send_results="false")
        self.assertFalse(result["success"])
        self.strategy.search.assert_not_awaited()
        self.controller._send_search_results.assert_not_awaited()

    async def test_send_failure_keeps_evidence_and_releases_thumbnail_bytes(self):
        async def fill(items):
            for candidate in items:
                candidate.thumbnail_bytes = b"image"

        self.controller.service._fill_thumbnails.side_effect = fill
        self.controller._send_search_results.side_effect = RuntimeError("platform offline")
        result = await self.search(send_results=True)
        self.assertTrue(result["success"])
        self.assertFalse(result["message_sent"])
        self.assertEqual(len(result["items"]), 2)
        self.assertIn("RuntimeError", result["send_error"])
        sent_items = self.controller._send_search_results.call_args.args[1]
        self.assertTrue(all(candidate.thumbnail_bytes is None for candidate in sent_items))

    async def test_invalid_id_types_do_not_select_latest(self):
        for value in ("", " ", 42, []):
            with self.subTest(value=value):
                result = json.loads(
                    await self.controller.tool_search_image(
                        self.event, image_id=value, image_index=-1
                    )
                )
                self.assertFalse(result["success"])
        self.strategy.search.assert_not_awaited()

    async def test_command_always_sends_even_with_silent_tool_default(self):
        result = await self.controller._run_command_search(
            self.event, "https://example.com/a.jpg", None
        )
        self.assertIsNone(result)
        self.event.send.assert_awaited_once()
        self.controller._send_search_results.assert_awaited_once()

    async def test_internal_visual_review_is_cached_and_does_not_send(self):
        self.controller.config["ai_behavior"]["visual_evidence_enabled"] = True
        verdict = {
            "status": "ok",
            "candidates": [{"index": 2, "relation": "contains_target", "region": "右侧"}],
        }
        with patch.object(ctl, "review_evidence", AsyncMock(return_value=verdict)) as reviewer:
            first = await self.search()
            await self.search()
        self.assertEqual(first["visual_evidence"], verdict)
        reviewer.assert_awaited_once()
        self.event.send.assert_not_awaited()

    async def test_photo_intent_uses_web_engine_and_explicit_engine_still_wins(self):
        sauce = Strategy("SauceNAO", [item("sauce", "SauceNAO")])
        self.controller.strategies.append(sauce)
        self.controller.service = AliceImageReverseService(self.controller.strategies)
        result = await self.search(intent="真人照片识别")
        self.assertEqual(result["used_strategies"], ["Google Lens"])
        result = await self.search(intent="真人照片识别", strategies="saucenao")
        self.assertEqual(result["selection_mode"], "explicit")
        self.assertEqual(result["used_strategies"], ["saucenao"])

    async def test_engine_errors_are_not_reported_as_proof_of_no_match(self):
        self.strategy.search.side_effect = RuntimeError("HTTP 503")
        result = await self.search()
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "搜索服务暂时不可用")
        self.assertEqual(result["failed_strategies"], ["Google Lens"])


class VisualEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_gets_aligned_images_without_page_titles_or_tools(self):
        out = io.BytesIO()
        Image.new("RGB", (24, 16), "red").save(out, "PNG")
        response = SimpleNamespace(
            completion_text=json.dumps(
                {
                    "candidates": [
                        {
                            "index": 2,
                            "relation": "contains_target",
                            "confidence": 0.9,
                            "region": "右侧",
                            "reason": "同一局部画面",
                        },
                    ]
                }
            )
        )
        provider = SimpleNamespace(text_chat=AsyncMock(return_value=response))
        context = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="p"),
            get_provider_by_id=lambda _: provider,
        )
        candidates = [item("misleading_title"), item("specific_source")]
        with patch.object(
            ev, "download_bytes", AsyncMock(side_effect=[out.getvalue(), None, out.getvalue()])
        ):
            result = await ev.review_evidence(
                context,
                SimpleNamespace(unified_msg_origin="chat"),
                "https://example.com/query.jpg",
                candidates,
                {},
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"][0]["index"], 2)
        self.assertIn(1, result["not_compared_indices"])
        kwargs = provider.text_chat.call_args.kwargs
        self.assertEqual(len(kwargs["image_urls"]), 2)
        self.assertIsNone(kwargs["func_tool"])
        self.assertNotIn("misleading_title", kwargs["prompt"])
        self.assertNotIn("base64", json.dumps(result))

        # Providers may expose text through a message chain instead of completion_text.
        provider.text_chat.return_value = SimpleNamespace(
            completion_text="",
            result_chain=SimpleNamespace(get_plain_text=lambda: response.completion_text),
        )
        with patch.object(ev, "download_bytes", AsyncMock(return_value=out.getvalue())):
            fallback = await ev.review_evidence(
                context,
                SimpleNamespace(unified_msg_origin="chat"),
                "https://example.com/query.jpg",
                candidates,
                {},
            )
        self.assertEqual(fallback["status"], "ok")
        self.assertEqual(fallback["candidates"][0]["index"], 2)

    def test_invalid_verdict_never_becomes_verified(self):
        for text in (
            "{}",
            '{"candidates":[]}',
            '{"candidates":[{"index":99,"relation":"same_image"}]}',
        ):
            self.assertEqual(ev.parse_visual_evidence(text, [1])["status"], "unavailable")
        parsed = ev.parse_visual_evidence(
            '{"candidates":[{"index":1,"relation":"related","confidence":99}]}', [1]
        )
        self.assertIsNone(parsed["candidates"][0]["confidence"])

    def test_source_diversity_keeps_specific_second_google_hit(self):
        candidates = [item(str(i), "SauceNAO", 0.99 - i * 0.01) for i in range(8)]
        candidates += [item("unrelated_forum", score=0.6), item("specific_video", score=0.4)]
        selected = ev.select_evidence(candidates, 4)
        self.assertIn("specific_video", [x.title for x in selected])

    def test_duplicate_page_retains_individual_title_thumbnail_pairs(self):
        first = item("forum", "SauceNAO", 0.99)
        second = item("video", "Google Lens", 0.4)
        second.url = first.url
        second.thumbnail = "https://example.com/second.jpg"
        merged = merge_and_rank([first, second], 5)
        payload = ev.evidence_payload(merged)[0]
        self.assertEqual([r["title"] for r in payload["evidence"]], ["forum", "video"])
        self.assertEqual(payload["evidence"][1]["thumbnail_url"], second.thumbnail)

    def test_binary_thumbnail_is_never_in_tool_result(self):
        candidate = item("test")
        candidate.thumbnail = "base64://private-image-content"
        candidate.thumbnail_bytes = b"private-image-content"
        self.assertNotIn("private-image-content", json.dumps(ev.evidence_payload([candidate])))


class ImageSelectionTests(unittest.TestCase):
    def test_recapturing_url_keeps_stable_id(self):
        images = SessionImages()
        first = images.add_image("https://example.com/a.jpg", "m1")
        second = images.add_image("https://example.com/a.jpg", "m2")
        self.assertEqual(first.image_id, second.image_id)
        self.assertEqual(second.message_id, "m2")

    def test_reading_empty_sessions_does_not_evict_images_and_bot_ids_isolate(self):
        manager = ImageContextManager(max_sessions=1)
        event = SimpleNamespace(unified_msg_origin="bot-a:group:1", session_id="1")
        manager.add_image(event, "https://example.com/a.jpg")
        other = SimpleNamespace(unified_msg_origin="bot-b:group:1", session_id="1")
        self.assertIsNone(manager.get_recent_image(other))
        self.assertEqual(manager.get_recent_image(event), "https://example.com/a.jpg")


class ReverseToolAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_send_is_forwarded_without_bool_coercion(self):
        plugin = SimpleNamespace(tool_reverse_image=AsyncMock(return_value="{}"))
        tool = AliceReverseImageTool(plugin=plugin)
        context = SimpleNamespace(context=SimpleNamespace(event=object()))
        await tool.call(context, image_id="id")
        self.assertIsNone(plugin.tool_reverse_image.call_args.kwargs["send_results"])
        await tool.call(context, image_id="id", send_results=False)
        self.assertIs(plugin.tool_reverse_image.call_args.kwargs["send_results"], False)
