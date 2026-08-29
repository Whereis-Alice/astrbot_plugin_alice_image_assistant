"""WebUI 后端与纯逻辑层的单测。

覆盖三块：

* `alice_image/webui/` 的主题 / 指令目录 / 配置表单纯逻辑；
* `webapi.py` 的图片缓存、参数夹取、消息解析与界面偏好归一化；
* 前后端契约一致性：指令表与 `main.py` 的装饰器、图标与 sprite、主题与 CSS。

这些用例全部不出网、不依赖 AstrBot 运行时，可以在 CI 里裸跑。
"""

from __future__ import annotations

import io
import re
import time
import unittest
from pathlib import Path
from typing import Any

from astrbot_plugin_alice_image_assistant import webapi
from astrbot_plugin_alice_image_assistant.alice_image.webui.commands import (
    command_catalog,
    command_count,
    command_names,
)
from astrbot_plugin_alice_image_assistant.alice_image.webui.schema import (
    MASK,
    apply_changes,
    build_groups,
    coerce_value,
    count_fields,
    find_node,
    load_schema,
)
from astrbot_plugin_alice_image_assistant.alice_image.webui.themes import (
    THEMES,
    default_theme_key,
    is_valid_theme,
    normalize_theme,
    theme_payload,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PAGE_DIR = PLUGIN_ROOT / "pages" / "alice"


def _read(relative: str) -> str:
    return (PLUGIN_ROOT / relative).read_text(encoding="utf-8")


def _sprite_icons() -> set[str]:
    """index.html 内联 sprite 里真实存在的图标名（去掉 i- 前缀）。"""

    html = _read("pages/alice/index.html")
    return {match.group(1) for match in re.finditer(r'<symbol id="i-([a-z0-9-]+)"', html)}


def _png_bytes(width: int = 8, height: int = 6) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (120, 90, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


class _RootConfig(dict):
    """模拟 AstrBot 的 AstrBotConfig：dict + save_config()。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self) -> None:
        self.saved += 1


class _FakeOrchestrator:
    def __init__(self, available: set[str]) -> None:
        self._available_keys = available

    def _available(self, source: str) -> bool:
        return source in self._available_keys


class _FakeReverseService:
    def get_available_strategies(self) -> list[str]:
        return ["SauceNAO", "Google Lens"]


class _FakePlugin:
    """只提供 AliceWebService 会 getattr 的那几个属性。"""

    def __init__(self, raw_config: dict[str, Any] | None = None) -> None:
        self.raw_config = raw_config if raw_config is not None else _RootConfig()
        self.context = None
        self.find_config = self.raw_config.get("find_image", {})
        self.reverse_config = self.raw_config.get("reverse_image", {})
        self.forward = _FakeOrchestrator({"soutu", "pixiv"})
        self.reverse = type("_Ctl", (), {"service": _FakeReverseService()})()
        self.kv: dict[str, Any] = {}

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        return self.kv.get(key, default)

    async def put_kv_data(self, key: str, value: Any) -> None:
        self.kv[key] = value


def _service(raw_config: dict[str, Any] | None = None) -> webapi.AliceWebService:
    return webapi.AliceWebService(_FakePlugin(raw_config), plugin_dir=PLUGIN_ROOT)


class ThemeTests(unittest.TestCase):
    def test_themes_are_unique_and_well_formed(self) -> None:
        keys = [theme.key for theme in THEMES]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertGreaterEqual(len(keys), 6)
        for theme in THEMES:
            with self.subTest(theme=theme.key):
                self.assertRegex(theme.key, r"^[a-z][a-z0-9_]*$")
                self.assertTrue(theme.label.strip())
                self.assertTrue(theme.label_en.strip())
                self.assertTrue(theme.mood.strip())
                self.assertRegex(theme.accent, r"^#[0-9a-fA-F]{6}$")

    def test_normalize_theme_falls_back(self) -> None:
        self.assertEqual(normalize_theme("aurora"), "aurora")
        self.assertEqual(normalize_theme("does-not-exist"), default_theme_key())
        self.assertEqual(normalize_theme(None), default_theme_key())
        self.assertEqual(normalize_theme(123), default_theme_key())
        self.assertFalse(is_valid_theme("AURORA"))

    def test_theme_payload_matches_themes(self) -> None:
        payload = theme_payload()
        self.assertEqual(len(payload), len(THEMES))
        self.assertEqual([item["key"] for item in payload], [t.key for t in THEMES])
        for item in payload:
            self.assertEqual(
                set(item), {"key", "label", "label_en", "accent", "mood"}
            )

    def test_every_theme_has_css_block(self) -> None:
        css = _read("pages/alice/theme.css")
        for theme in THEMES:
            with self.subTest(theme=theme.key):
                self.assertIn(f'[data-alice-theme="{theme.key}"]', css)

    def test_frontend_fallback_themes_match_backend(self) -> None:
        app_js = _read("pages/alice/app.js")
        block = re.search(r"FALLBACK_THEMES = \[(.*?)\];", app_js, re.S)
        self.assertIsNotNone(block)
        keys = re.findall(r'key: "([a-z0-9_]+)"', block.group(1))
        self.assertEqual(keys, [theme.key for theme in THEMES])

    def test_style_css_has_no_raw_colors(self) -> None:
        """style.css 只允许用 var(--…)，颜色字面量必须留在 theme.css 里。"""

        css = _read("pages/alice/style.css")
        stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", stripped), [])
        self.assertEqual(re.findall(r"\brgba?\(", stripped), [])


class CommandCatalogTests(unittest.TestCase):
    def test_counts_are_consistent(self) -> None:
        groups = command_catalog()
        flat = [cmd for group in groups for cmd in group["commands"]]
        self.assertEqual(command_count(), len(flat))
        self.assertEqual(command_count(), len(command_names()))

    def test_command_names_are_unique(self) -> None:
        names = command_names()
        self.assertEqual(len(names), len(set(names)))

    def test_catalog_matches_main_decorators(self) -> None:
        """指令表必须和 main.py 的 @filter.command 完全对齐（双向差集为空）。"""

        source = _read("main.py")
        declared = {
            "/" + match.group(1)
            for match in re.finditer(r'@filter\.command\(\s*"([^"]+)"', source)
        }
        catalog = set(command_names())
        self.assertEqual(catalog - declared, set(), "指令表里有 main.py 未注册的指令")
        self.assertEqual(declared - catalog, set(), "main.py 有指令未写进指令表")

    def test_every_command_is_documented(self) -> None:
        for group in command_catalog():
            with self.subTest(group=group["key"]):
                self.assertTrue(group["label"].strip())
                self.assertTrue(group["desc"].strip())
                self.assertTrue(group["commands"])
            for cmd in group["commands"]:
                with self.subTest(cmd=cmd["cmd"]):
                    self.assertTrue(cmd["cmd"].startswith("/"))
                    self.assertTrue(cmd["usage"].startswith(cmd["cmd"]))
                    self.assertTrue(cmd["desc"].strip())
                    self.assertIsInstance(cmd["tags"], list)

    def test_group_icons_exist_in_sprite(self) -> None:
        icons = _sprite_icons()
        for group in command_catalog():
            with self.subTest(group=group["key"]):
                self.assertIn(group["icon"], icons)

    def test_config_group_icons_exist_in_sprite(self) -> None:
        from astrbot_plugin_alice_image_assistant.alice_image.webui import schema

        icons = _sprite_icons()
        for path, (icon, desc) in schema._GROUP_META.items():
            with self.subTest(path=path):
                self.assertIn(icon, icons)
                self.assertTrue(desc.strip())

    def test_frontend_known_icons_match_sprite(self) -> None:
        app_js = _read("pages/alice/app.js")
        block = re.search(r"KNOWN_ICONS = new Set\(\[(.*?)\]\);", app_js, re.S)
        self.assertIsNotNone(block)
        declared = set(re.findall(r'"([a-z0-9-]+)"', block.group(1)))
        self.assertEqual(declared, _sprite_icons())


class ConfigFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = load_schema(PLUGIN_ROOT)

    def test_schema_is_covered_by_groups(self) -> None:
        """build_groups 必须把 schema 里每个叶子字段都渲染出来，不能漏项。"""

        def count_leaves(items: dict[str, Any]) -> int:
            total = 0
            for node in items.values():
                if not isinstance(node, dict):
                    continue
                if str(node.get("type")) == "object":
                    total += count_leaves(node.get("items") or {})
                else:
                    total += 1
            return total

        groups = build_groups(self.schema, {})
        self.assertEqual(count_fields(groups), count_leaves(self.schema))
        self.assertGreater(count_fields(groups), 100)

    def test_top_level_groups(self) -> None:
        groups = build_groups(self.schema, {})
        self.assertEqual(
            [group["key"] for group in groups],
            ["find_image", "reverse_image", "webui"],
        )
        for group in groups:
            with self.subTest(group=group["key"]):
                self.assertTrue(group["label"].strip())
                self.assertTrue(group["desc"].strip())

    def test_sensitive_fields_never_leak(self) -> None:
        secret = "super-secret-token"
        config = {
            "find_image": {"pixiv": {"settings": {"refresh_token": secret}}},
            "reverse_image": {"api_keys": {"saucenao_api_key": secret}},
        }
        groups = build_groups(self.schema, config)

        def walk(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for group in nodes:
                out.extend(group.get("fields") or [])
                out.extend(walk(group.get("groups") or []))
            return out

        fields = walk(groups)
        passwords = [field for field in fields if field["kind"] == "password"]
        self.assertGreaterEqual(len(passwords), 3)
        filled = [field for field in passwords if field["filled"]]
        self.assertEqual(len(filled), 2)
        for field in passwords:
            with self.subTest(path=field["path"]):
                self.assertNotEqual(field["value"], secret)
                self.assertIn(field["value"], {"", MASK})
                self.assertNotEqual(field["default"], secret)

    def test_find_node_resolves_leaves_only(self) -> None:
        self.assertIsNotNone(find_node(self.schema, "find_image.enabled"))
        self.assertIsNotNone(find_node(self.schema, "webui.enabled"))
        # 分组节点本身不是可写字段
        self.assertIsNone(find_node(self.schema, "find_image"))
        self.assertIsNone(find_node(self.schema, "find_image.pixiv.settings"))
        # 不存在的路径
        self.assertIsNone(find_node(self.schema, "find_image.nope"))
        self.assertIsNone(find_node(self.schema, "find_image.enabled.deeper"))
        self.assertIsNone(find_node(self.schema, ""))

    def test_coerce_bool_accepts_common_spellings(self) -> None:
        node = {"type": "bool", "default": False}
        for raw in (True, "true", "TRUE", "on", "yes", "1", 1, "是", "开启"):
            with self.subTest(raw=raw):
                self.assertEqual(coerce_value(node, raw), (True, True, ""))
        for raw in (False, "false", "off", "no", "0", 0, ""):
            with self.subTest(raw=raw):
                self.assertEqual(coerce_value(node, raw), (True, False, ""))

    def test_coerce_number_clamps_and_rejects(self) -> None:
        node = {"type": "int", "default": 5, "slider": {"min": 1, "max": 10, "step": 1}}
        self.assertEqual(coerce_value(node, "999")[1], 10)
        self.assertEqual(coerce_value(node, -40)[1], 1)
        self.assertEqual(coerce_value(node, "3.6")[1], 4)
        ok, _, reason = coerce_value(node, "abc")
        self.assertFalse(ok)
        self.assertTrue(reason)
        ok, _, reason = coerce_value(node, True)
        self.assertFalse(ok)
        self.assertTrue(reason)
        ok, _, reason = coerce_value(node, "inf")
        self.assertFalse(ok)

    def test_coerce_list_from_text(self) -> None:
        node = {"type": "list", "default": []}
        ok, value, _ = coerce_value(node, "a, b\nc,,  ")
        self.assertTrue(ok)
        self.assertEqual(value, ["a", "b", "c"])
        ok, value, _ = coerce_value(node, ["x", " y ", ""])
        self.assertTrue(ok)
        self.assertEqual(value, ["x", "y"])

    def test_coerce_options_whitelist(self) -> None:
        node = {"type": "string", "default": "a", "options": ["a", "b"]}
        self.assertEqual(coerce_value(node, "b")[1], "b")
        ok, _, reason = coerce_value(node, "c")
        self.assertFalse(ok)
        self.assertIn("a", reason)

    def test_coerce_sensitive_mask_is_rejected(self) -> None:
        node = {"type": "string", "default": "", "is_sensitive": True}
        ok, _, reason = coerce_value(node, MASK)
        self.assertFalse(ok)
        self.assertIn("掩码", reason)
        self.assertEqual(coerce_value(node, "real")[1], "real")

    def test_apply_changes_writes_nested_and_reports(self) -> None:
        root: dict[str, Any] = {}
        applied, rejected = apply_changes(
            self.schema,
            root,
            {
                "find_image.enabled": "false",
                "webui.preview_ttl_seconds": 999999,
                "find_image.pixiv.settings.refresh_token": MASK,
                "find_image.nope.deeper": 1,
                "": 1,
                "find_image": {},
            },
        )
        self.assertEqual(
            sorted(applied), ["find_image.enabled", "webui.preview_ttl_seconds"]
        )
        self.assertIs(root["find_image"]["enabled"], False)
        self.assertEqual(root["webui"]["preview_ttl_seconds"], 7200)
        reasons = {item["path"]: item["reason"] for item in rejected}
        self.assertEqual(
            set(reasons),
            {
                "find_image.pixiv.settings.refresh_token",
                "find_image.nope.deeper",
                "",
                "find_image",
            },
        )
        self.assertIn("掩码", reasons["find_image.pixiv.settings.refresh_token"])
        self.assertIn("未知配置项", reasons["find_image.nope.deeper"])

    def test_apply_changes_does_not_clobber_siblings(self) -> None:
        root: dict[str, Any] = {"find_image": {"enabled": True, "keep": "me"}}
        applied, rejected = apply_changes(
            self.schema, root, {"find_image.enabled": False}
        )
        self.assertEqual(applied, ["find_image.enabled"])
        self.assertEqual(rejected, [])
        self.assertEqual(root["find_image"]["keep"], "me")


class ImageCacheTests(unittest.TestCase):
    def test_put_and_get_roundtrip(self) -> None:
        cache = webapi.ImageCache()
        entry = cache.put(b"1234", mime="image/png", width=4, height=2, label="x")
        self.assertEqual(len(cache), 1)
        self.assertEqual(cache.total_bytes, 4)
        self.assertEqual(cache.get(entry.token).data, b"1234")
        self.assertTrue(entry.filename.endswith(".png"))

    def test_empty_payload_is_refused(self) -> None:
        cache = webapi.ImageCache()
        with self.assertRaises(webapi.AliceWebError):
            cache.put(b"", mime="image/png")

    def test_unknown_token_raises_404(self) -> None:
        cache = webapi.ImageCache()
        with self.assertRaises(webapi.AliceWebError) as ctx:
            cache.get("nope")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_max_items_evicts_oldest(self) -> None:
        cache = webapi.ImageCache(max_items=2)
        first = cache.put(b"a", mime="image/png")
        cache.put(b"b", mime="image/png")
        cache.put(b"c", mime="image/png")
        self.assertEqual(len(cache), 2)
        with self.assertRaises(webapi.AliceWebError):
            cache.get(first.token)

    def test_get_refreshes_lru_order(self) -> None:
        cache = webapi.ImageCache(max_items=2)
        first = cache.put(b"a", mime="image/png")
        second = cache.put(b"b", mime="image/png")
        cache.get(first.token)  # first 变成最近使用
        cache.put(b"c", mime="image/png")
        self.assertEqual(cache.get(first.token).data, b"a")
        with self.assertRaises(webapi.AliceWebError):
            cache.get(second.token)

    def test_max_bytes_evicts(self) -> None:
        cache = webapi.ImageCache(max_bytes=10)
        cache.put(b"x" * 8, mime="image/png")
        cache.put(b"y" * 8, mime="image/png")
        self.assertEqual(len(cache), 1)
        self.assertLessEqual(cache.total_bytes, 10)

    def test_ttl_expiry(self) -> None:
        cache = webapi.ImageCache(ttl_seconds=30)
        entry = cache.put(b"abc", mime="image/png")
        cache.purge(now=time.monotonic() + 31)
        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.total_bytes, 0)
        with self.assertRaises(webapi.AliceWebError):
            cache.get(entry.token)

    def test_zero_ttl_disables_expiry(self) -> None:
        cache = webapi.ImageCache(ttl_seconds=0)
        entry = cache.put(b"abc", mime="image/png")
        cache.purge(now=time.monotonic() + 10_000)
        self.assertEqual(cache.get(entry.token).data, b"abc")

    def test_clear_releases_everything(self) -> None:
        cache = webapi.ImageCache()
        cache.put(b"abc", mime="image/png")
        cache.put(b"defg", mime="image/jpeg")
        cache.clear()
        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.total_bytes, 0)
        self.assertEqual(cache.stats()["count"], 0)

    def test_stats_reports_limits(self) -> None:
        cache = webapi.ImageCache(ttl_seconds=120, max_items=7, max_bytes=999)
        stats = cache.stats()
        self.assertEqual(stats["ttl_seconds"], 120)
        self.assertEqual(stats["max_items"], 7)
        self.assertEqual(stats["max_bytes"], 999)
