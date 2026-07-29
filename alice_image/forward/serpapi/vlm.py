"""VLM 选图：把网格拼接图交给视觉模型，返回选中的编号。"""

from __future__ import annotations

import asyncio
import base64
import json
import re

from astrbot.api import logger
from astrbot.api.provider import Provider

# 单张拼图保留的图片比例（候选数 >10 时按比例取，至少 1 张）
SELECTION_RATIO = 0.12


def _normalize_indices(
    raw: list[int], total_images_count: int, max_selection: int
) -> list[int]:
    """把原始编号裁剪到合法范围、保序去重，并截断到 max_selection 张。

    VLM 不一定遵守 prompt 的数量约束，故在输出端强制裁剪——"该选几张"以 max_selection
    为唯一可信来源（决赛圈"砍一半"的收敛逻辑依赖于此）。
    """
    valid = [n for n in raw if 1 <= n <= total_images_count]
    return list(dict.fromkeys(valid))[:max_selection]


async def select_from_collage(
    image_bytes: bytes,
    prompt: str,
    total_images_count: int,
    vlm_provider: Provider,
    max_selection: int | None = None,
) -> list[int]:
    """让 VLM 从网格图中挑选匹配的图片，返回 1-based 编号列表（裁剪到 1..total_images_count）。

    ``max_selection`` 为本批最大选出张数：调用方显式指定时以其为准，留空则按
    SELECTION_RATIO 估算，确保"该选几张"只有单一来源。
    """
    base64_str = base64.b64encode(image_bytes).decode("utf-8")
    image_url = f"base64://{base64_str}"

    if max_selection is None:
        max_selection = (
            1
            if total_images_count <= 10
            else max(1, round(total_images_count * SELECTION_RATIO))
        )
    else:
        max_selection = max(1, min(max_selection, total_images_count))

    prompt_template = (
        "This is a grid of images, each with a numeric label. Please observe each "
        "labeled image carefully.\n"
        f"Based on the following description: '{prompt}', identify all matching images.\n\n"
        f"You must select a maximum of {max_selection} image(s).\n\n"
        'Your response MUST be a JSON object containing a single key "selected_indices".\n'
        "The value should be a list of the numeric labels of all matching images.\n"
        "Do not include any other text, explanations, or markdown formatting "
        "outside of the JSON object.\n\n"
        "Example of a valid response:\n"
        '{"selected_indices": [1, 5, 8]}'
    )

    retries = 3
    for attempt in range(retries):
        try:
            response = await vlm_provider.text_chat(
                prompt=prompt_template, image_urls=[image_url]
            )
            result = response.completion_text or ""
            logger.debug(f"[alice_image_serpapi] VLM 原始响应: '{result}'")

            try:
                json_match = re.search(r"\{.*\}", result, re.DOTALL)
                if json_match:
                    data = json.loads(json_match.group(0))
                    selected = data.get("selected_indices")
                    if isinstance(selected, list):
                        nums = [
                            int(n)
                            for n in selected
                            if isinstance(n, (int, str)) and str(n).isdigit()
                        ]
                        return _normalize_indices(
                            nums, total_images_count, max_selection
                        )
            except (json.JSONDecodeError, AttributeError):
                logger.debug("[alice_image_serpapi] VLM JSON 解析失败，改用正则兜底")

            # 正则兜底：仅保留落在合法编号范围内的数字，避免误抓年份/分辨率等
            numbers = [int(n) for n in re.findall(r"\d+", result)]
            return _normalize_indices(numbers, total_images_count, max_selection)

        except Exception as e:  # noqa: BLE001 - 重试
            logger.warning(
                f"[alice_image_serpapi] VLM 调用第 {attempt + 1}/{retries} 次失败: {e}"
            )
            if attempt < retries - 1:
                await asyncio.sleep(2)

    logger.error("[alice_image_serpapi] VLM 调用全部失败")
    return []
