"""把 `_conf_schema.json` 转成 WebUI 可渲染的表单，并安全地写回改动。

设计要点：

* **单一数据源**：字段的类型、默认值、范围、选项全部来自 `_conf_schema.json`，
  WebUI 不再重复维护一份，避免两边漂移。
* **敏感字段不出网**：`is_sensitive` 的值一律以掩码返回，只告知"是否已填写"。
  写回时若收到的仍是掩码常量，视为"未修改"直接拒绝，防止误清空凭据。
* **写回前校验**：类型强转 + slider 范围裁剪 + options 白名单 + list 元素清洗，
  任何不合法的路径都进 `rejected`，不会污染配置文件。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, MutableMapping
from pathlib import Path
from typing import Any

MASK = "********"
"""敏感字段对外展示用的掩码常量。"""

_KIND_BY_TYPE = {
    "bool": "bool",
    "int": "int",
    "float": "float",
    "string": "text",
    "text": "textarea",
    "list": "list",
    "object": "object",
}

# 分组图标与展示顺序：key 为配置路径，值为前端 SVG sprite 的 id。
_GROUP_META: dict[str, tuple[str, str]] = {
    "find_image": ("search", "文字找图的总开关、来源选择与视觉复核。"),
    "find_image.llm_review": ("eye", "由视觉模型复核候选图，决定最终发哪张。"),
    "find_image.query_rewrite": ("wand", "把用户口语化描述改写成更适合各来源的检索词。"),
    "find_image.pixiv": ("palette", "Pixiv 账号、功能开关与画质参数。"),
    "find_image.pixiv.features": ("toggles", "逐条开关 Pixiv 指令与工具能力。"),
    "find_image.pixiv.settings": ("sliders", "Pixiv 运行参数：凭据、过滤阈值、代理与发送方式。"),
    "find_image.soutu": ("compass", "搜图源：主站抓取 + Bing 兜底 + 视觉挑图。"),
    "find_image.serpapi": ("globe", "SerpApi Google 图片搜索。"),
    "reverse_image": ("history", "以图搜图的总开关、会话图片上下文与展示方式。"),
    "reverse_image.ai_behavior": ("brain", "LLM 如何记住与引用会话里的图片。"),
    "reverse_image.command": ("terminal", "聊天指令相关的等待与超时。"),
    "reverse_image.network": ("plug", "出网代理、UA 与文件访问许可。"),
    "reverse_image.api_keys": ("key", "各溯源引擎的凭据（仅掩码展示）。"),
    "reverse_image.strategies": ("layers", "启用哪些溯源引擎及其阈值。"),
    "reverse_image.display": ("image", "结果条数与合并转发。"),
    "webui": ("layout", "WebUI 自身的开关与限额。"),
}


def load_schema(plugin_dir: str | Path) -> dict[str, Any]:
    """读取插件根目录下的 `_conf_schema.json`。"""

    path = Path(plugin_dir) / "_conf_schema.json"
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return {}


def _node_kind(node: Mapping[str, Any]) -> str:
    raw_type = str(node.get("type", "string"))
    kind = _KIND_BY_TYPE.get(raw_type, "text")
    if kind == "text" and node.get("is_sensitive"):
        return "password"
    if kind == "text" and node.get("options"):
        return "select"
    return kind


def _label_of(key: str, node: Mapping[str, Any]) -> str:
    description = node.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()
    return key


def _field_payload(
    path: str,
    key: str,
    node: Mapping[str, Any],
    value: Any,
) -> dict[str, Any]:
    kind = _node_kind(node)
    default = node.get("default")
    payload: dict[str, Any] = {
        "path": path,
        "key": key,
        "kind": kind,
        "label": _label_of(key, node),
        "desc": str(node.get("hint") or ""),
        "default": MASK if (kind == "password" and default) else default,
    }

    if kind == "password":
        payload["value"] = MASK if value else ""
        payload["filled"] = bool(value)
        payload["mask"] = MASK
    elif kind == "list":
        payload["value"] = [str(item) for item in value] if isinstance(value, list) else []
    else:
        payload["value"] = value

    options = node.get("options")
    if isinstance(options, list) and options:
        payload["options"] = [str(item) for item in options]

    slider = node.get("slider")
    if isinstance(slider, Mapping):
        for bound in ("min", "max", "step"):
            if bound in slider:
                payload[bound] = slider[bound]

    return payload


def _walk(
    schema_items: Mapping[str, Any],
    config: Mapping[str, Any],
    prefix: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 `(fields, groups)`：叶子字段与嵌套子分组。"""

    fields: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []

    for key, node in schema_items.items():
        if not isinstance(node, Mapping):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if str(node.get("type")) == "object":
            child_items = _as_mapping(node.get("items"))
            child_config = _as_mapping(config.get(key))
            child_fields, child_groups = _walk(child_items, child_config, path)
            icon, fallback_desc = _GROUP_META.get(path, ("folder", ""))
            groups.append(
                {
                    "key": path,
                    "label": _label_of(key, node),
                    "desc": str(node.get("hint") or fallback_desc),
                    "icon": icon,
                    "fields": child_fields,
                    "groups": child_groups,
                }
            )
            continue

        raw_value = config.get(key, node.get("default"))
        fields.append(_field_payload(path, key, node, raw_value))

    return fields, groups


def build_groups(
    schema: Mapping[str, Any],
    config: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """把整份 schema 转成 `/config` 需要的分组结构。"""

    fields, groups = _walk(schema, config, "")
    if fields:
        # 理论上顶层全是 object 节，留一个兜底分组以防将来加平铺字段。
        groups.insert(
            0,
            {
                "key": "_root",
                "label": "通用",
                "desc": "",
                "icon": "settings",
                "fields": fields,
                "groups": [],
            },
        )
    return groups


def count_fields(groups: Iterable[Mapping[str, Any]]) -> int:
    """统计分组树里的叶子字段总数。"""

    total = 0
    for group in groups:
        total += len(group.get("fields") or [])
        total += count_fields(group.get("groups") or [])
    return total


def find_node(schema: Mapping[str, Any], path: str) -> Mapping[str, Any] | None:
    """按 `a.b.c` 路径定位 schema 叶子节点。"""

    parts = [part for part in path.split(".") if part]
    if not parts:
        return None
    cursor: Mapping[str, Any] = schema
    node: Mapping[str, Any] | None = None
    for index, part in enumerate(parts):
        candidate = cursor.get(part)
        if not isinstance(candidate, Mapping):
            return None
        node = candidate
        if index < len(parts) - 1:
            if str(candidate.get("type")) != "object":
                return None
            cursor = _as_mapping(candidate.get("items"))
    if node is not None and str(node.get("type")) == "object":
        return None
    return node


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "开启", "是"}
    return bool(value)


def _clamp_number(value: float, node: Mapping[str, Any]) -> float:
    slider = node.get("slider")
    if isinstance(slider, Mapping):
        low = slider.get("min")
        high = slider.get("max")
        if isinstance(low, (int, float)) and value < low:
            value = low
        if isinstance(high, (int, float)) and value > high:
            value = high
    return value


def coerce_value(node: Mapping[str, Any], value: Any) -> tuple[bool, Any, str]:
    """把前端传来的值按 schema 强转并校验。

    返回 `(ok, coerced, reason)`；`ok` 为 False 时 `reason` 是中文原因。
    """

    raw_type = str(node.get("type", "string"))

    if raw_type == "bool":
        return True, _coerce_bool(value), ""

    if raw_type in {"int", "float"}:
        if isinstance(value, bool):
            return False, None, "布尔值不能写入数值字段"
        try:
            number = float(str(value).strip())
        except (TypeError, ValueError):
            return False, None, "不是合法数字"
        if not math.isfinite(number):
            return False, None, "不是有限数字"
        number = _clamp_number(number, node)
        return True, (round(number) if raw_type == "int" else float(number)), ""

    if raw_type == "list":
        if isinstance(value, str):
            items = [line.strip() for line in value.replace(",", "\n").split("\n")]
        elif isinstance(value, Iterable):
            items = [str(item).strip() for item in value]
        else:
            return False, None, "不是合法列表"
        return True, [item for item in items if item], ""

    # 其余按字符串处理
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    if node.get("is_sensitive") and text == MASK:
        return False, None, "敏感字段未修改（收到掩码）"
    options = node.get("options")
    if isinstance(options, list) and options:
        allowed = [str(item) for item in options]
        if text not in allowed:
            return False, None, f"取值必须是 {'、'.join(allowed)} 之一"
    return True, text, ""


def _ensure_branch(root: MutableMapping[str, Any], parts: list[str]) -> MutableMapping[str, Any]:
    cursor: MutableMapping[str, Any] = root
    for part in parts:
        child = cursor.get(part)
        if not isinstance(child, MutableMapping):
            child = {}
            cursor[part] = child
        cursor = child
    return cursor


def apply_changes(
    schema: Mapping[str, Any],
    config_root: MutableMapping[str, Any],
    changes: Mapping[str, Any],
) -> tuple[list[str], list[dict[str, str]]]:
    """把一批 `{路径: 值}` 写回配置对象。

    只在内存里写，调用方负责随后 `save_config()`。
    """

    applied: list[str] = []
    rejected: list[dict[str, str]] = []

    for path, value in changes.items():
        if not isinstance(path, str) or not path:
            rejected.append({"path": str(path), "reason": "路径为空"})
            continue
        node = find_node(schema, path)
        if node is None:
            rejected.append({"path": path, "reason": "未知配置项"})
            continue
        ok, coerced, reason = coerce_value(node, value)
        if not ok:
            rejected.append({"path": path, "reason": reason})
            continue
        parts = path.split(".")
        branch = _ensure_branch(config_root, parts[:-1])
        branch[parts[-1]] = coerced
        applied.append(path)

    return applied, rejected
