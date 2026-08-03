from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from astrbot_plugin_alice_image_assistant.alice_image.pixiv.handlers.user import (
    UserHandler,
)
from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.config import (
    PixivConfig,
)


class _Client:
    def user_detail(self, *_args, **_kwargs):
        return None

    def user_illusts(self, *_args, **_kwargs):
        return None

    @staticmethod
    def parse_qs(_url):
        return {}


class _ClientWrapper:
    def __init__(self, illusts) -> None:
        self.client_api = _Client()
        self.illusts = illusts

    async def authenticate(self) -> bool:
        return True

    async def call_pixiv_api(self, method, *_args, **_kwargs):
        if method.__name__ == "user_detail":
            return SimpleNamespace(user=SimpleNamespace(name="测试画师"))
        if method.__name__ == "user_illusts":
            return SimpleNamespace(illusts=self.illusts, next_url=None)
        raise AssertionError(f"unexpected method: {method.__name__}")


class PixivUserCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_artist_random_filters_configured_tags_before_sampling(self) -> None:
        illusts = [
            SimpleNamespace(
                id=1,
                tags=[SimpleNamespace(name="blocked", translated_name="")],
            ),
            SimpleNamespace(
                id=2,
                tags=[SimpleNamespace(name="safe", translated_name="禁止")],
            ),
            SimpleNamespace(
                id=3,
                tags=[SimpleNamespace(name="safe", translated_name="安全")],
            ),
        ]
        config = PixivConfig(
            {
                "return_count": 3,
                "artist_random_blocked_tags": ["BLOCKED", "禁止"],
                "artist_random_pages": 1,
                "show_filter_result": True,
            }
        )
        handler = UserHandler(_ClientWrapper(illusts), config)
        event = SimpleNamespace(plain_result=lambda text: text)
        captured = {}

        async def fake_process(items, filter_config, *_args, **_kwargs):
            captured["ids"] = [item.id for item in items]
            captured["return_count"] = filter_config.return_count
            yield "sent"

        with patch(
            "astrbot_plugin_alice_image_assistant.alice_image.pixiv.handlers.user.process_and_send_illusts",
            new=fake_process,
        ):
            results = [
                result
                async for result in handler.pixiv_user_random(
                    event,
                    "29872901",
                    1,
                )
            ]

        self.assertEqual(captured["ids"], [3])
        self.assertEqual(captured["return_count"], 1)
        self.assertEqual(results[-1], "sent")
        self.assertIn("过滤 2 个作品", results[0])


if __name__ == "__main__":
    unittest.main()
