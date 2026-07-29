from __future__ import annotations

import unittest
from unittest.mock import Mock

from astrbot_plugin_alice_image_assistant.main import AliceImageAssistantPlugin


class DisabledLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_modules_disabled_initialize_and_terminate_without_resources(
        self,
    ) -> None:
        context = Mock()
        plugin = AliceImageAssistantPlugin(
            context,
            {
                "find_image": {"enabled": False},
                "reverse_image": {"enabled": False},
            },
        )

        self.assertIsNone(plugin.forward)
        self.assertIsNone(plugin.pixiv)
        self.assertIsNone(plugin.reverse)
        context.add_llm_tools.assert_not_called()

        await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
