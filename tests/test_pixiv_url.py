from __future__ import annotations

import unittest
from types import SimpleNamespace

from astrbot_plugin_alice_image_assistant.alice_image.pixiv.handlers.illust import (
    IllustHandler,
)
from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.url import (
    extract_pixiv_artwork_id,
)


class PixivUrlTests(unittest.TestCase):
    def test_extracts_modern_links_from_natural_messages(self) -> None:
        cases = {
            "https://www.pixiv.net/artworks/12345678": "12345678",
            "帮我发这张：https://www.pixiv.net/en/artworks/23456789?utm_source=share#1。": "23456789",
            "看看 pixiv.net/zh-cn/artworks/34567890/ 可以吗": "34567890",
            "https://m.pixiv.net/artworks/45678901": "45678901",
            "看这张https://www.pixiv.net/artworks/56789012。好可爱": "56789012",
        }

        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(extract_pixiv_artwork_id(message), expected)

    def test_extracts_legacy_artwork_link(self) -> None:
        message = (
            "https://www.pixiv.net/member_illust.php?mode=medium&illust_id=56789012"
        )
        self.assertEqual(extract_pixiv_artwork_id(message), "56789012")

    def test_uses_only_the_first_valid_artwork_link(self) -> None:
        message = (
            "https://www.pixiv.net/artworks/11111111 "
            "https://www.pixiv.net/artworks/22222222"
        )
        self.assertEqual(extract_pixiv_artwork_id(message), "11111111")

    def test_rejects_non_artwork_or_lookalike_links(self) -> None:
        cases = (
            "https://evilpixiv.net/artworks/12345678",
            "https://www.pixiv.net/users/12345678",
            "https://www.pixiv.net/artworks/123abc",
            "普通消息里没有链接",
        )

        for message in cases:
            with self.subTest(message=message):
                self.assertIsNone(extract_pixiv_artwork_id(message))


class PixivUrlHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_uses_the_detected_link_from_the_given_message(self) -> None:
        handler = IllustHandler(
            SimpleNamespace(client_api=object()),
            SimpleNamespace(pixiv_urlsearch_enabled=True),
        )
        received_ids: list[str] = []

        async def pixiv_specific(_event, illust_id: str):
            received_ids.append(illust_id)
            yield illust_id

        handler.pixiv_specific = pixiv_specific
        event = SimpleNamespace(message_str="没有 Pixiv 链接")
        message = "帮我发 https://www.pixiv.net/en/artworks/67890123?foo=bar"

        results = [result async for result in handler.pixiv_msg_url(event, message)]

        self.assertEqual(received_ids, ["67890123"])
        self.assertEqual(results, ["67890123"])


if __name__ == "__main__":
    unittest.main()
