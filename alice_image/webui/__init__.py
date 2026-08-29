"""WebUI 支撑模块：主题、指令目录与配置表单的纯逻辑层。

本包只做数据准备，不依赖任何 Web 框架，方便单测与在缺少
`astrbot.api.web` 的环境下复用。
"""

from __future__ import annotations

from .commands import command_catalog, command_count
from .themes import THEMES, default_theme_key, theme_payload

__all__ = [
    "THEMES",
    "command_catalog",
    "command_count",
    "default_theme_key",
    "theme_payload",
]
