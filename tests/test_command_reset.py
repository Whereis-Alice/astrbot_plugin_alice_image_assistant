from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import star_handlers_registry

from astrbot_plugin_alice_image_assistant.alice_image.pixiv.utils.help import (
    replace_public_command_names,
)
from astrbot_plugin_alice_image_assistant.alice_image.reverse.constant import (
    REVERSE_SEARCH_COMMAND,
)
from astrbot_plugin_alice_image_assistant.alice_image.webui.commands import (
    command_catalog,
    command_names,
)
from astrbot_plugin_alice_image_assistant.main import AliceImageAssistantPlugin

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_COMMANDS = {
    "图片帮助",
    "找图",
    "搜图",
    "谷歌搜图",
    "识图",
    "插画",
    "最新插画",
    "推荐插画",
    "插画并搜",
    "作品详情",
    "插画榜",
    "相关插画",
    "深度插画",
    "插画评论",
    "插画特辑",
    "画师",
    "画师详情",
    "画师作品",
    "随机插画",
    "画师找图",
    "小说",
    "推荐小说",
    "最新小说",
    "小说系列",
    "小说评论",
    "下载小说",
    "订阅画师",
    "退订画师",
    "画师订阅",
    "插画帮助",
    "随机添加",
    "随机删除",
    "随机列表",
    "随机暂停",
    "随机开启",
    "随机状态",
    "随机执行",
    "榜单添加",
    "榜单删除",
    "榜单列表",
    "趋势标签",
    "生成图设置",
    "插画设置",
    "热门插画",
    "赞助画师",
    "赞助帖子",
    "赞助推荐",
    "赞助搜索",
}


class CommandResetTests(unittest.TestCase):
    def test_public_command_set_is_exactly_48_and_has_no_legacy_prefix(self) -> None:
        source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        names = re.findall(r'@filter\.command\("([^"]+)"', source)
        declared = set(names)

        self.assertEqual(len(names), len(declared))
        self.assertEqual(len(declared), 48)
        self.assertEqual(declared, EXPECTED_COMMANDS)
        self.assertEqual(
            {name[1:] for name in command_names()},
            EXPECTED_COMMANDS,
        )
        for name in declared:
            self.assertRegex(name, r"^[\u4e00-\u9fff]+$")

    def test_reverse_command_constant_matches_registered_route(self) -> None:
        self.assertEqual(REVERSE_SEARCH_COMMAND, "识图")
        source = (PLUGIN_ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command("识图")', source)

    def test_pixiv_help_is_rendered_with_current_public_names(self) -> None:
        help_data = json.loads(
            (PLUGIN_ROOT / "alice_image/pixiv/data/helpmsg.json").read_text(encoding="utf-8")
        )
        rendered = "\n".join(
            replace_public_command_names(value)
            for value in help_data.values()
            if isinstance(value, str)
        )

        self.assertIn("/插画 ", rendered)
        self.assertIn("/画师作品 ", rendered)
        self.assertIn("/随机插画 ", rendered)
        self.assertNotRegex(rendered, r"/aa(?:P|F)?|/pixiv(?:_|\s)")

    def test_every_catalog_usage_is_in_readme_and_builtin_help(self) -> None:
        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        with (PLUGIN_ROOT / "alice_image/pixiv/data/helpmsg.json").open(
            encoding="utf-8", newline=""
        ) as stream:
            raw_help = stream.read()
        self.assertNotIn("\r", raw_help, "帮助文件必须使用 LF 换行")
        help_text = json.loads(raw_help)["pixiv_help"]
        for group in command_catalog():
            for command in group["commands"]:
                with self.subTest(command=command["cmd"]):
                    usage = command["usage"]
                    self.assertIn("`" + usage.replace("|", r"\|") + "`", readme)
                    if group["key"] != "core":
                        self.assertIn("`" + usage + "`", help_text)

    def test_runtime_filters_route_exact_names_and_parse_arguments(self) -> None:
        filters = [
            item
            for handler in star_handlers_registry.star_handlers_map.values()
            if handler.handler_module_path == AliceImageAssistantPlugin.__module__
            for item in handler.event_filters
            if isinstance(item, CommandFilter)
        ]
        self.assertEqual(len(filters), 48)
        self.assertFalse(any(item.alias for item in filters))
        for name in EXPECTED_COMMANDS:
            with self.subTest(command=name):
                event = SimpleNamespace(
                    is_at_or_wake_command=True,
                    get_message_str=lambda name=name: name,
                    set_extra=lambda *_args: None,
                )
                matched = [item.command_name for item in filters if item.filter(event, {})]
                self.assertEqual(matched, [name])

        for name in ("aa", "aaP", "aa溯", "pixiv", "插画评论错字"):
            event.get_message_str = lambda name=name: name
            self.assertFalse(any(item.filter(event, {}) for item in filters), name)

        parsed = {}
        event.get_message_str = lambda: "插画评论 12345 10"
        event.set_extra = parsed.__setitem__
        matched = [item for item in filters if item.filter(event, {})]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].handler_md.handler_name, "pixiv_comments")
        self.assertEqual(parsed["parsed_params"], {"illust_id": "12345", "offset": "10"})


if __name__ == "__main__":
    unittest.main()
