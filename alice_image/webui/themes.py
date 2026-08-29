"""WebUI 主题定义（前后端唯一数据源）。

前端 `pages/alice/theme.css` 里的 CSS 变量按 `key` 命名为
`[data-alice-theme="wonderland"]` 之类的选择器；此处的列表决定主题切换器
里出现哪些选项、顺序以及强调色小圆点的颜色。新增主题时必须同时补
CSS，否则前端会回退到默认主题。
"""

from __future__ import annotations

from typing import Any, Final


class Theme:
    """一套主题的元信息。"""

    __slots__ = ("accent", "key", "label", "label_en", "mood")

    def __init__(
        self,
        key: str,
        label: str,
        label_en: str,
        accent: str,
        mood: str,
    ) -> None:
        self.key = key
        self.label = label
        self.label_en = label_en
        self.accent = accent
        self.mood = mood

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "label_en": self.label_en,
            "accent": self.accent,
            "mood": self.mood,
        }


THEMES: Final[tuple[Theme, ...]] = (
    Theme("wonderland", "奇境", "Wonderland", "#d4af37", "深紫底 + 香槟金，插件默认气质"),
    Theme("aurora", "极光", "Aurora", "#3ddc97", "墨蓝底 + 极光绿，适合长时间盯屏"),
    Theme("midnight", "午夜", "Midnight", "#7c8cff", "近黑底 + 蓝紫霓虹，纯暗色控制台风"),
    Theme("sakura", "樱雪", "Sakura", "#e8749a", "浅色底 + 樱粉，柔和亮色"),
    Theme("paper", "纸稿", "Paper", "#4a6fa5", "米白纸感 + 靛蓝，最高可读性"),
    Theme("sunset", "落日", "Sunset", "#ff8a4c", "暖褐底 + 橙红，低蓝光"),
)

_THEME_KEYS: Final[frozenset[str]] = frozenset(theme.key for theme in THEMES)


def default_theme_key() -> str:
    """默认主题 key。"""

    return THEMES[0].key


def is_valid_theme(key: Any) -> bool:
    """判断前端传回的主题 key 是否受支持。"""

    return isinstance(key, str) and key in _THEME_KEYS


def normalize_theme(key: Any) -> str:
    """把任意输入收敛为合法主题 key。"""

    return key if is_valid_theme(key) else default_theme_key()


def theme_payload() -> list[dict[str, Any]]:
    """供 `/meta` 返回的主题列表。"""

    return [theme.to_dict() for theme in THEMES]
