from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_alice_image_assistant.alice_image.config import NestedConfigProxy
from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.config import (
    PixivConfig,
)
from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.help import (
    replace_public_command_names,
)
from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.tag import (
    item_has_any_exact_tag,
)
from astrbot_plugin_alice_image_assistant.alice_image.tools import (
    AliceFindImageTool,
    AlicePixivNovelTool,
    AliceReverseImageTool,
    AliceSessionImagesTool,
)
from astrbot_plugin_alice_image_assistant.main import AliceImageAssistantPlugin

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class _RootConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self) -> None:
        self.saved += 1


class ConfigAndToolTests(unittest.TestCase):
    def test_schema_has_exactly_two_public_groups(self) -> None:
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        self.assertEqual(list(schema), ["find_image", "reverse_image"])
        self.assertEqual(schema["find_image"]["description"], "找图模块")
        self.assertEqual(schema["reverse_image"]["description"], "以图搜图模块")

    def test_every_pixiv_feature_is_boolean_and_defaulted(self) -> None:
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        features = schema["find_image"]["items"]["pixiv"]["items"]["features"]["items"]
        self.assertGreaterEqual(len(features), 30)
        self.assertIn("artist_search", features)
        self.assertIn("artist_random", features)
        for name, item in features.items():
            with self.subTest(name=name):
                self.assertEqual(item["type"], "bool")
                self.assertIsInstance(item["default"], bool)

    def test_nested_proxy_persists_runtime_pixiv_changes(self) -> None:
        root = _RootConfig({"find_image": {"pixiv": {"settings": {"return_count": 1}}}})
        proxy = NestedConfigProxy(root, ("find_image", "pixiv", "settings"))
        proxy["return_count"] = 3
        proxy.save_config()
        self.assertEqual(root["find_image"]["pixiv"]["settings"]["return_count"], 3)
        self.assertEqual(root.saved, 1)

    def test_pixiv_runtime_quality_default_matches_schema(self) -> None:
        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        schema_default = schema["find_image"]["items"]["pixiv"]["items"]["settings"][
            "items"
        ]["image_quality"]["default"]

        self.assertEqual(schema_default, "medium")
        self.assertEqual(PixivConfig({}).image_quality, schema_default)

    def test_pixiv_command_return_count_can_be_overridden_once(self) -> None:
        config = PixivConfig({"return_count": 3})

        self.assertEqual(config.resolve_return_count(), 3)
        self.assertEqual(config.resolve_return_count(1), 1)
        self.assertEqual(config.return_count, 3)
        self.assertEqual(PixivConfig({"return_count": 99}).return_count, 10)

    def test_artist_random_config_is_normalized(self) -> None:
        config = PixivConfig(
            {
                "artist_random_blocked_tags": [" R-18 ", "r-18", None, "AI"],
                "artist_random_pages": 99,
            }
        )

        self.assertEqual(config.artist_random_blocked_tags, ["R-18", "AI"])
        self.assertEqual(config.artist_random_pages, 10)

    def test_pixiv_search_selection_defaults_and_bounds(self) -> None:
        default_config = PixivConfig({})
        bounded_config = PixivConfig({"recent_dedup_retention_days": 999})

        self.assertTrue(default_config.randomize_search_results)
        self.assertTrue(default_config.recent_dedup_enabled)
        self.assertEqual(default_config.recent_dedup_retention_days, 7)
        self.assertEqual(bounded_config.recent_dedup_retention_days, 90)

        schema = json.loads((PLUGIN_ROOT / "_conf_schema.json").read_text("utf-8"))
        settings = schema["find_image"]["items"]["pixiv"]["items"]["settings"][
            "items"
        ]
        self.assertTrue(settings["randomize_search_results"]["default"])
        self.assertTrue(settings["recent_dedup_enabled"]["default"])

    def test_artist_random_blocked_tags_match_exact_name_or_translation(self) -> None:
        item = SimpleNamespace(
            tags=[
                SimpleNamespace(name="R-18", translated_name="成人向"),
                {"name": "original", "translated_name": "原创"},
            ]
        )

        self.assertTrue(item_has_any_exact_tag(item, ["r-18"]))
        self.assertTrue(item_has_any_exact_tag(item, ["原创"]))
        self.assertFalse(item_has_any_exact_tag(item, ["R-1"]))

    def test_llm_tool_names_and_schemas_are_unique(self) -> None:
        tools = [
            AliceFindImageTool(plugin=object()),
            AlicePixivNovelTool(plugin=object()),
            AliceSessionImagesTool(plugin=object()),
            AliceReverseImageTool(plugin=object()),
        ]
        names = [tool.name for tool in tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(name.startswith("alice_image_") for name in names))
        for tool in tools:
            self.assertEqual(tool.parameters["type"], "object")
            self.assertIn("properties", tool.parameters)
        find_properties = tools[0].parameters["properties"]
        self.assertIn("artist_name", find_properties)
        self.assertIn("pixiv_user_id", find_properties)

    def test_no_upstream_public_command_or_tool_identifiers_remain(self) -> None:
        source = (PLUGIN_ROOT / "main.py").read_text("utf-8")
        self.assertNotRegex(source, r'@filter\.command\("搜图"')
        self.assertNotRegex(source, r'@filter\.command\("pixiv')
        for forbidden in (
            'name: str = "search_image_tool"',
            'name: str = "pixiv_search_illust"',
            '@llm_tool("get_session_images")',
            '@llm_tool("search_image")',
        ):
            self.assertNotIn(forbidden, source)
        command_names = re.findall(r'@filter\.command\("([^"]+)"', source)
        self.assertEqual(len(command_names), len(set(command_names)))
        self.assertTrue(all(name.startswith("aa") for name in command_names))
        self.assertNotIn('@filter.command("爱图', source)
        self.assertNotIn('alias=["alice-', source)

    def test_optional_pixiv_command_count_preserves_multi_word_query(self) -> None:
        parser = AliceImageAssistantPlugin._parse_query_count

        self.assertEqual(parser("星之卡比 1"), ("星之卡比", 1, ""))
        self.assertEqual(parser("初音ミク 冬"), ("初音ミク 冬", None, ""))
        query, count, error = parser("星之卡比 11")
        self.assertEqual((query, count), ("星之卡比", None))
        self.assertIn("1-10", error)

    def test_pixiv_help_examples_use_public_commands(self) -> None:
        message = (
            "`/pixiv 初音ミク` `/pixiv_user_search 米山舞` "
            "https://pypi.org/project/pixivpy3/"
        )
        replaced = replace_public_command_names(message)

        self.assertIn("`/aaP 初音ミク`", replaced)
        self.assertIn("`/aaP画师 米山舞`", replaced)
        self.assertIn("/project/pixivpy3/", replaced)


if __name__ == "__main__":
    unittest.main()
