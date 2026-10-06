from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.api.message_components import Image

from astrbot_plugin_alice_image_assistant.alice_image.reverse import (
    ascii2d_strategy as ascii2d,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse import (
    controller as ctl,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse import (
    google_lens_strategy as lens,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse import (
    sauce_nao_strategy as sauce,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse import (
    utils,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.models import (
    ExplorationResult,
    ProviderSearchError,
    ProviderSearchOutcome,
    SearchResultItem,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.service import (
    AliceImageReverseService,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.yandex_strategy import YandexStrategy
from astrbot_plugin_alice_image_assistant.webapi import AliceWebService

from .test_google_lens_evidence import Response, Session
from .test_webapi import _FakePlugin


class ProviderDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_sauce_low_similarity_notice_is_request_scoped(self):
        strategy = sauce.SauceNaoStrategy(api_key=" key ")
        session = Session(
            [Response({"results": [{"header": {"similarity": "35"}}]}), Response({"results": []})]
        )
        with patch.object(sauce, "get_aiohttp_session", AsyncMock(return_value=session)):
            filtered = await strategy.search("https://example.com/query.jpg")
            empty = await strategy.search("https://example.com/other.jpg")
        self.assertIsInstance(filtered, ProviderSearchOutcome)
        self.assertIn("60%", filtered.notices[0])
        self.assertEqual(empty, [])

    async def test_sauce_api_error_with_results_is_still_a_failure(self):
        session = Session(
            [Response({"header": {"status": -1, "message": "secret"}, "results": []})]
        )
        with (
            patch.object(sauce, "get_aiohttp_session", AsyncMock(return_value=session)),
            self.assertRaises(ProviderSearchError) as raised,
        ):
            await sauce.SauceNaoStrategy(api_key="key").search("https://example.com/a.jpg")
        self.assertNotIn("secret", str(raised.exception))

    async def test_sauce_rejects_nonfinite_similarity(self):
        for value in ("NaN", "Infinity", -1, 101):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sauce.SauceNaoStrategy()._parse_node({"header": {"similarity": value}})

    async def test_lens_network_failure_does_not_try_other_keys(self):
        strategy = lens.GoogleLensStrategy(api_keys=["a", "b", "c"])
        session = Session([Response({}, 503)])
        with (
            patch.object(lens, "get_aiohttp_session", AsyncMock(return_value=session)),
            self.assertRaises(ProviderSearchError),
        ):
            await strategy.search("https://example.com/a.jpg")
        self.assertEqual(len(session.urls), 1)
        self.assertEqual(strategy._quota_cache, {})

    async def test_ascii_preserves_successful_page_and_reports_failed_page(self):
        strategy = ascii2d.Ascii2dStrategy()
        strategy._fetch_authenticity_token = AsyncMock(return_value="token")
        strategy._post_url_search = AsyncMock(return_value="https://ascii2d.net/search/color/123")
        item = SearchResultItem(title="good", url="https://example.com/good")
        strategy._fetch_and_parse_result_page = AsyncMock(
            side_effect=[[item], ProviderSearchError("bad")]
        )
        outcome = await strategy.search("https://example.com/a.jpg")
        self.assertEqual([result.url for result in outcome.items], [item.url])
        self.assertIn("特征", outcome.notices[0])
        strategy._fetch_and_parse_result_page.side_effect = ProviderSearchError("bad")
        with self.assertRaises(ProviderSearchError):
            await strategy.search("https://example.com/a.jpg")

    async def test_ascii_redirect_to_home_or_wrong_host_is_not_zero_matches(self):
        strategy = ascii2d.Ascii2dStrategy()
        for final in ("https://ascii2d.net/", "https://bad.example/search/color/123"):
            with self.subTest(final=final):
                session = SimpleNamespace(
                    get=AsyncMock(
                        return_value=SimpleNamespace(
                            status_code=200, url=final, text="<html>challenge</html>"
                        )
                    )
                )
                strategy._get_session = AsyncMock(return_value=session)
                with self.assertRaises(ProviderSearchError):
                    await strategy._fetch_and_parse_result_page(
                        "https://ascii2d.net/search/color/123", False
                    )

    async def test_yandex_challenge_is_failure_and_valid_empty_is_zero_matches(self):
        strategy = YandexStrategy()
        strategy._request_html = AsyncMock(return_value=("<html>captcha</html>", 200))
        with self.assertRaises(ProviderSearchError):
            await strategy.search("https://example.com/a.jpg")
        with patch.object(
            strategy,
            "_extract_data_state",
            return_value=json.dumps({"initialState": {"cbirSites": {"sites": []}}}),
        ):
            self.assertEqual(await strategy.search("https://example.com/a.jpg"), [])

    async def test_service_keeps_filter_notices_and_failures_separate(self):
        def strategy(name, value=None, error=None):
            return SimpleNamespace(
                get_service_name=lambda: name,
                search=AsyncMock(return_value=value, side_effect=error),
            )

        service = AliceImageReverseService(
            [
                strategy("SauceNAO", ProviderSearchOutcome(notices=["低相似度已过滤"])),
                strategy("Ascii2d", error=ProviderSearchError("offline")),
            ]
        )
        outcome = await service.explore("https://example.com/a.jpg", download_thumbnails=False)
        self.assertFalse(outcome.all_failed)
        self.assertEqual(outcome.failed_strategies, ["Ascii2d"])
        self.assertEqual(outcome.notices, ["低相似度已过滤"])


class CommandAndToolDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controller = ctl.AliceReverseController(
            Mock(),
            {
                "strategies": {
                    f"enable_{x}": False for x in ("google_lens", "saucenao", "ascii2d", "yandex")
                },
                "ai_behavior": {"visual_evidence_enabled": False},
            },
        )
        self.event = SimpleNamespace(
            unified_msg_origin="regression:group:1",
            session_id="1",
            message_obj=SimpleNamespace(message_id="m1"),
            send=AsyncMock(),
            plain_result=lambda x: x,
            get_messages=list,
        )
        self.controller._send_search_results = AsyncMock()

    async def asyncTearDown(self):
        await self.controller.terminate()

    async def test_raw_fallback_cannot_select_second_image(self):
        first, second = (
            Image(file="file:///missing.jpg"),
            Image(file="https://example.com/second.jpg"),
        )
        self.event.get_messages = lambda: [first, second]
        self.event.message_obj.raw_message = {
            "message": [
                {"type": "image", "data": {"file": "first"}},
                {"type": "image", "data": {"url": "https://example.com/second.jpg"}},
            ]
        }
        with patch.object(ctl, "get_http_image_url", AsyncMock(return_value=None)):
            result = await self.controller._run_command_search(self.event, first, None)
        self.assertEqual(result, "获取图片失败")
        self.assertEqual(self.controller._get_raw_image_urls(self.event, first_only=True), [])

    async def test_first_image_raw_url_is_used_but_not_for_reply(self):
        first = Image(file="file:///first.jpg")
        self.event.get_messages = lambda: [first]
        self.event.message_obj.raw_message = {
            "message": [{"type": "image", "data": {"url": "https://example.com/first.jpg"}}]
        }
        self.controller.service.explore = AsyncMock(return_value=ExplorationResult())
        await self.controller._run_command_search(self.event, first, None)
        self.assertEqual(
            self.controller.service.explore.call_args.args[0], "https://example.com/first.jpg"
        )
        self.controller.service.explore.reset_mock()
        with patch.object(ctl, "get_http_image_url", AsyncMock(return_value=None)):
            await self.controller._run_command_search(
                self.event, Image(file="file:///reply.jpg"), None
            )
        self.controller.service.explore.assert_not_awaited()

    async def test_notices_reach_silent_tool_and_command(self):
        notice = "低相似度候选已过滤"
        strategy = SimpleNamespace(
            get_service_name=lambda: "SauceNAO",
            search=AsyncMock(return_value=ProviderSearchOutcome(notices=[notice])),
            close=AsyncMock(),
        )
        self.controller.strategies = [strategy]
        self.controller.service = AliceImageReverseService([strategy])
        manager = ctl.get_image_context_manager()
        manager.add_image(self.event, "https://example.com/input.jpg")
        result = json.loads(await self.controller.tool_search_image(self.event, image_index=-1))
        self.assertEqual(result["notices"], [notice])
        self.event.send.assert_not_awaited()
        command = await self.controller._run_command_search(
            self.event, "https://example.com/input.jpg", None
        )
        self.assertIn(notice, command)

    async def test_all_engine_failure_is_visible_in_command(self):
        self.controller.service.explore = AsyncMock(
            return_value=ExplorationResult(
                attempted_strategies=["Google Lens"], failed_strategies=["Google Lens"]
            )
        )
        message = await self.controller._run_command_search(
            self.event, "https://example.com/a.jpg", None
        )
        self.assertIn("服务暂时不可用", message)

    async def test_only_consumed_waited_image_stops_normal_chat(self):
        self.event.get_sender_id = lambda: "user"
        self.event.stop_event = Mock()
        image = Image(file="https://example.com/a.jpg")
        await self.controller._consume_image_wait(self.event, image)
        self.event.stop_event.assert_not_called()
        state = await self.controller._set_image_wait(self.event, None)
        await self.controller._consume_image_wait(self.event, image)
        self.event.stop_event.assert_called_once()
        self.assertIs(state.future.result().image, image)

    def test_strategy_separators_and_canonical_lens_name(self):
        for value in ("google yandex", "google，yandex", "google、yandex", "Google Lens;yandex"):
            self.assertEqual(self.controller._split_strategy_names(value), ["google", "yandex"])


class ImageInputTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_uri_spaces_and_size_limit(self):
        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(utils, "_allow_local_file_access", True),
            patch.object(utils, "MAX_IMAGE_BYTES", 4),
        ):
            path = Path(root) / "a b.jpg"
            path.write_bytes(b"1234")
            self.assertEqual(await utils.read_image_bytes(path.as_uri()), b"1234")
            path.write_bytes(b"12345")
            self.assertIsNone(await utils.read_image_bytes(path.as_uri()))
            self.assertIsNone(utils._read_file_bytes(root))

    async def test_unc_and_remote_file_uris_never_reach_disk_read(self):
        with (
            patch.object(utils, "_allow_local_file_access", True),
            patch.object(utils, "_read_file_bytes") as reader,
        ):
            for source in (
                "file://server/share/a.png",
                "file:////server/share/a.png",
                "//server/share/a.png",
                r"\\server\share\a.png",
            ):
                self.assertIsNone(await utils.read_image_bytes(source))
            reader.assert_not_called()

    async def test_streamed_download_cannot_exceed_limit_without_content_length(self):
        async def chunks(_size):
            yield b"1234"
            yield b"5678"

        response = Response({})
        response.content_length = None
        response.content = SimpleNamespace(iter_chunked=chunks)
        with patch.object(
            utils, "get_aiohttp_session", AsyncMock(return_value=Session([response]))
        ):
            self.assertIsNone(
                await utils.download_bytes("https://example.com/image.jpg", max_bytes=5)
            )

    def test_proxy_credentials_and_encoded_query_keys_are_redacted(self):
        safe = utils._sanitize_url_for_logging(
            "http://name:secret@proxy.test:8080/?%61pi_key=hidden#private"
        )
        for secret in ("name", "secret", "hidden", "private"):
            self.assertNotIn(secret, safe)
        self.assertIn("proxy.test:8080", safe)

    def test_credentials_are_trimmed_deduplicated_and_invalid_entries_removed(self):
        self.assertEqual(
            lens.GoogleLensStrategy(api_keys=[None, " a ", "", "a", 3, " b\n"]).api_keys, ["a", "b"]
        )
        self.assertEqual(utils.normalize_credentials(" a\nb "), ["a", "b"])
        self.assertEqual(ascii2d.Ascii2dStrategy(session_id=" token\n").session_id, "token")


class WebDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_reverse_notices_reach_webui(self):
        plugin = _FakePlugin()
        plugin.reverse.service.resolve_strategy_names = lambda _: ([], [])
        plugin.reverse.service.explore = AsyncMock(
            return_value=ExplorationResult(
                notices=["低相似度已过滤"], failed_strategies=["Ascii2d"]
            )
        )
        service = AliceWebService(plugin)
        result = await service.reverse({"image_url": "https://example.com/input.jpg"})
        self.assertIn("低相似度已过滤", result["notices"])
        self.assertNotIn("低相似度已过滤", result["errors"])
        self.assertTrue(any("Ascii2d" in message for message in result["errors"]))
