from __future__ import annotations

import unittest

from astrbot_plugin_alice_image_assistant.alice_image.reverse.models import (
    SearchResultItem,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.service import (
    AliceImageReverseService,
)


class _Strategy:
    def __init__(self, name: str, count: int) -> None:
        self.name = name
        self.count = count

    def get_service_name(self) -> str:
        return self.name

    async def search(self, _image_url: str):
        return [
            SearchResultItem(
                title=f"{self.name}-{index}",
                url=f"https://example.com/{self.name}/{index}",
                source=self.name,
            )
            for index in range(self.count)
        ]


class ReverseResultLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_max_results_is_applied_per_engine(self) -> None:
        service = AliceImageReverseService(
            [_Strategy("one", 5), _Strategy("two", 5)],
            max_results=2,
        )
        result = await service.explore("https://example.com/input.jpg")
        self.assertEqual(len(result.items), 4)
        self.assertEqual(
            [item.source for item in result.items], ["one", "one", "two", "two"]
        )


if __name__ == "__main__":
    unittest.main()
