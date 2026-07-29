"""配置读取与嵌套配置持久化辅助。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, Mapping):
        return dict(value.items())
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def section(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key, {})
    return as_dict(value)


class NestedConfigProxy(dict[str, Any]):
    """让上游的平面配置管理器可安全持久化到合集的嵌套分组。"""

    def __init__(self, root: Any, path: tuple[str, ...]) -> None:
        self.root = root
        self.path = path
        value: Any = root
        for key in path:
            value = value.get(key, {}) if hasattr(value, "get") else {}
        super().__init__(as_dict(value))

    def save_config(self) -> None:
        cursor: Any = self.root
        for key in self.path[:-1]:
            child = cursor.get(key) if hasattr(cursor, "get") else None
            if not isinstance(child, dict):
                child = {}
                cursor[key] = child
            cursor = child
        cursor[self.path[-1]] = dict(self)
        save = getattr(self.root, "save_config", None)
        if callable(save):
            save()
