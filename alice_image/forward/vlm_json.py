"""视觉模型 JSON 输出的宽容解析工具（纯函数，不依赖网络与 provider）。

把解析逻辑集中到一处，可以在升级 prompt（要求返回 confidence / reason）的同时
继续兼容旧格式（只有 best_index、裸数字、被 markdown 围栏包裹），
避免"prompt 变强但解析变脆"导致可用候选被误判成技术错误。
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json_objects(text: str) -> list[str]:
    """扫描花括号配对，抽出文本里所有顶层 JSON 对象片段。

    比正则更可靠：能穿透 markdown 围栏、前后寒暄、以及多段输出。
    """
    results: list[str] = []
    depth = 0
    start = -1
    in_string = False
    escape_next = False

    for i, char in enumerate(text):
        if not in_string:
            if char == '"':
                in_string = True
            elif char == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif char == "}" and depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    results.append(text[start : i + 1])
                    start = -1
        else:
            if escape_next:
                escape_next = False
            elif char == "\\":
                escape_next = True
            elif char == '"':
                in_string = False

    return results


def parse_json_payload(text: str, *, required_key: str = "") -> dict[str, Any] | None:
    """逆序取最后一个可解析的 JSON 对象；指定 required_key 时只接受含该键的对象。

    逆序是因为模型常先解释再给结论，最后一段才是最终答案。
    """
    if not text:
        return None
    for block in reversed(extract_json_objects(text)):
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if required_key and required_key not in data:
            continue
        return data
    return None


def coerce_confidence(value: Any) -> float | None:
    """把任意置信度写法收敛成 0..1 的小数；无法解析时返回 None（表示"未提供"）。

    模型经常写成百分数（86 / "86%"），直接当成 0..1 会误判为高置信度，故统一换算。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        cleaned = value.strip().rstrip("%")
        if not cleaned:
            return None
        percent = value.strip().endswith("%")
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
        if percent:
            parsed = parsed / 100.0
    else:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
    if parsed > 1.0:
        # 大于 1 的取值只可能是百分制，换算后再收敛，避免"90"被当成满分置信度。
        parsed = parsed / 100.0 if parsed <= 100.0 else 1.0
    return max(0.0, min(1.0, parsed))


def coerce_reason(value: Any, *, limit: int = 200) -> str:
    """把理由字段规整成单行短文本，避免把整段推理塞进日志与工具返回值。"""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "；".join(str(item) for item in value)
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit]


_TRUE_WORDS = {"true", "yes", "y", "1", "match", "matched", "是", "符合", "匹配"}
_FALSE_WORDS = {"false", "no", "n", "0", "mismatch", "不是", "不符合", "不匹配"}


def coerce_bool(value: Any) -> bool | None:
    """宽容解析布尔值：支持中文、英文与 0/1；无法判断时返回 None。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    return None


def coerce_index(value: Any) -> int | None:
    """把编号字段解析成整数；无法解析时返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else None
