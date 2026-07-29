from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from astrbot_plugin_alice_image_assistant.alice_image.config import NestedConfigProxy
from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.config import (
    PixivConfig,
)
from astrbot_plugin_alice_image_assistant.alice_image.tools import (
    AliceFindImageTool,
    AlicePixivNovelTool,
    AliceReverseImageTool,
    AliceSessionImagesTool,
)

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
        self.assertTrue(all(name.startswith("爱图") for name in command_names))


if __name__ == "__main__":
    unittest.main()
