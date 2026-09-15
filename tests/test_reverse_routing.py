from __future__ import annotations

import unittest

from astrbot_plugin_alice_image_assistant.alice_image.reverse.routing import (
    route_intent,
)

AVAILABLE = ("SauceNAO", "Google Lens", "Ascii2d")
AVAILABLE_WITH_YANDEX = (*AVAILABLE, "Yandex")


class ReverseIntentRoutingTests(unittest.TestCase):
    def test_source_intent_prefers_saucenao(self) -> None:
        route = route_intent("帮我找这张图的出处和画师", AVAILABLE)

        self.assertTrue(route.recognized)
        self.assertEqual(route.category, "source")
        self.assertEqual(route.strategy_names, ("SauceNAO",))
        self.assertIn("出处", route.matched_keywords)

    def test_similarity_intent_prefers_google_lens(self) -> None:
        route = route_intent("找相似图", AVAILABLE)

        self.assertEqual(route.category, "similar")
        self.assertEqual(route.strategy_names, ("Google Lens",))

    def test_anime_intent_uses_existing_ascii2d_only(self) -> None:
        route = route_intent("这是一张二次元插画", AVAILABLE)

        self.assertEqual(route.category, "anime")
        self.assertEqual(route.strategy_names, ("Ascii2d",))

    def test_unavailable_preferred_engine_falls_back(self) -> None:
        route = route_intent("找相似图片", ("SauceNAO", "Ascii2d"))

        self.assertEqual(route.strategy_names, ("Ascii2d",))

    def test_unrecognized_intent_preserves_all_strategy_behavior(self) -> None:
        route = route_intent("随便看看", AVAILABLE)

        self.assertFalse(route.recognized)
        self.assertEqual(route.strategy_names, ())

    def test_all_alias_returns_all_available_strategies(self) -> None:
        route = route_intent("全部", AVAILABLE)

        self.assertEqual(route.category, "all")
        self.assertEqual(route.strategy_names, AVAILABLE)

    def test_engine_alias_can_be_used_as_intent_fallback(self) -> None:
        route = route_intent("google lens", AVAILABLE)

        self.assertEqual(route.category, "explicit")
        self.assertEqual(route.strategy_names, ("Google Lens",))

    def test_english_intent_is_supported(self) -> None:
        route = route_intent("find a similar image", AVAILABLE)

        self.assertEqual(route.category, "similar")
        self.assertEqual(route.strategy_names, ("Google Lens",))

    def test_similarity_intent_prefers_yandex_when_available(self) -> None:
        route = route_intent("找相似图", AVAILABLE_WITH_YANDEX)

        self.assertEqual(route.strategy_names, ("Yandex",))

    def test_yandex_alias_can_be_selected_explicitly(self) -> None:
        route = route_intent("yandex images", AVAILABLE_WITH_YANDEX)

        self.assertEqual(route.category, "explicit")
        self.assertEqual(route.strategy_names, ("Yandex",))


if __name__ == "__main__":
    unittest.main()
