from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.message.components import Image

from astrbot_plugin_alice_image_assistant.alice_image.pixiv.core.client import (
    PixivClientWrapper,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.controller import (
    AliceReverseController,
)
from astrbot_plugin_alice_image_assistant.main import AliceImageAssistantPlugin


class BehavioralEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pixiv_command_forwards_optional_count(self) -> None:
        class _Pixiv:
            def __init__(self) -> None:
                self.calls = []

            async def pixiv_search_illust(self, event, tags, return_count):
                self.calls.append((event, tags, return_count))
                yield "sent"

        plugin = AliceImageAssistantPlugin.__new__(AliceImageAssistantPlugin)
        plugin.find_config = {
            "enabled": True,
            "commands_enabled": True,
            "pixiv": {
                "enabled": True,
                "features": {"illust_search": True},
            },
        }
        plugin.pixiv = _Pixiv()
        event = SimpleNamespace(
            message_str="aaP 星之卡比 1",
            plain_result=lambda text: text,
        )

        results = [result async for result in plugin.pixiv_search(event)]

        self.assertEqual(results, ["sent"])
        self.assertEqual(plugin.pixiv.calls, [(event, "星之卡比", 1)])

    def test_reverse_wait_recognizes_only_the_new_command(self) -> None:
        current = SimpleNamespace(
            is_at_or_wake_command=True,
            message_str="aa溯 google",
        )
        old = SimpleNamespace(
            is_at_or_wake_command=True,
            message_str="爱图溯 google",
        )

        self.assertTrue(AliceReverseController._is_search_command_event(current))
        self.assertFalse(AliceReverseController._is_search_command_event(old))

    async def test_find_tool_progress_message_is_opt_in(self) -> None:
        class _Event:
            def __init__(self) -> None:
                self.sent = []

            @staticmethod
            def plain_result(text: str) -> str:
                return text

            async def send(self, result) -> None:
                self.sent.append(result)

        class _Forward:
            async def search(self, *_args, **_kwargs):
                return SimpleNamespace(to_json=lambda: '{"success": true}')

        plugin = AliceImageAssistantPlugin.__new__(AliceImageAssistantPlugin)
        plugin.find_config = {"enabled": True, "llm_tools_enabled": True}
        plugin.forward = _Forward()
        event = _Event()

        await plugin.tool_find_image(
            event,
            query="星之卡比",
            description="星之卡比",
            source="auto",
            count=1,
            is_explanation=False,
        )

        self.assertEqual(event.sent, [])

        plugin.find_config["llm_search_progress_message_enabled"] = True
        await plugin.tool_find_image(
            event,
            query="星之卡比",
            description="星之卡比",
            source="auto",
            count=1,
            is_explanation=False,
        )

        self.assertEqual(event.sent, ["正在为你寻找「星之卡比」的图片..."])

    async def test_reverse_guidance_works_when_find_module_is_disabled(self) -> None:
        plugin = AliceImageAssistantPlugin.__new__(AliceImageAssistantPlugin)
        plugin.find_config = {"enabled": False}
        plugin.reverse_config = {
            "llm_tools_enabled": True,
            "inject_tool_guidance_enabled": True,
            "list_images_tool_enabled": False,
            "reverse_tool_enabled": True,
        }
        plugin.reverse = object()
        request = SimpleNamespace(system_prompt="system")

        await plugin.inject_tool_guidance(None, request)

        self.assertIn("alice_image_reverse_search", request.system_prompt)
        self.assertNotIn("alice_image_list_session_images", request.system_prompt)
        self.assertNotIn("alice_image_find", request.system_prompt)

    async def test_command_image_wait_still_consumes_images_when_context_is_off(
        self,
    ) -> None:
        controller = AliceReverseController(
            Mock(),
            {
                "ai_behavior": {"capture_image_context": False},
                "strategies": {
                    "enable_saucenao": False,
                    "enable_google_lens": False,
                    "enable_ascii2d": False,
                },
            },
        )
        image = Image(file="", url="https://example.com/input.jpg")
        event = Mock()
        event.get_messages.return_value = [image]
        event.message_obj = SimpleNamespace(message_id="message-1")
        event.get_sender_id.return_value = "user-1"
        event.is_at_or_wake_command = False
        event.message_str = ""
        event.unified_msg_origin = "test:group:room"
        controller._consume_image_wait = AsyncMock()

        await controller.on_message(event)

        controller._consume_image_wait.assert_awaited_once_with(event, image)
        await controller.terminate()

    def test_pixiv_client_construction_does_not_probe_network(self) -> None:
        config = SimpleNamespace(
            proxy="",
            api_proxy_host="",
            refresh_token="",
            refresh_interval=180,
            get_requests_kwargs=lambda: {},
        )
        with patch.object(
            PixivClientWrapper,
            "_require_appapi_hosts_with_cn_doh",
            side_effect=AssertionError("plugin construction must not access network"),
        ):
            client = PixivClientWrapper(config)

        self.assertIsNotNone(client.client_api)
        self.assertIsNone(client.start_refresh_task())


if __name__ == "__main__":
    unittest.main()
