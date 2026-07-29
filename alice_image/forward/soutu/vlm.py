# -*- coding: utf-8 -*-
import json
import re
import os
import tempfile
import textwrap
import asyncio
import random
import uuid
from typing import List

import aiofiles
from astrbot.api.provider import Provider
from astrbot.api import logger


def _extract_json_objects(text: str) -> List[str]:
    results = []
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
            elif char == "}":
                if depth > 0:
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


async def select_best_image_index(
    vlm_provider: Provider, image_bytes: bytes, description: str, total_count: int
) -> int:
    if total_count <= 0:
        return -1

    unique_filename = f"vlm_soutu_collage_{uuid.uuid4().hex}.jpg"
    temp_path = os.path.join(tempfile.gettempdir(), unique_filename)

    try:
        async with aiofiles.open(temp_path, "wb") as f:
            await f.write(image_bytes)
    except Exception as e:
        logger.error(f"写入临时拼图失败: {e}")
        # 🌟 修复状态混淆：写入失败直接返回特异性异常码 -2
        return -2

    safe_desc = description[:300].replace("```", "")

    prompt = textwrap.dedent(f"""
        这是一张包含了 {total_count} 张图片的拼图网格，每张图片左上角都有一个黑底白字的数字编号。
        请仔细观察，并根据视觉需求描述：“{safe_desc}”，选出最符合要求的一张图片。
        
        【规则要求】
        1. 如果所有图片均不符合上述需求，请严格返回数值 0。
        2. 若存在符合需求的图片，请返回对应的数字编号。
        
        输出格式限定：仅返回一个JSON对象，包含 "best_index" 键。
        
        示例：
        {{
          "best_index": 0
        }}
    """).strip()

    retries = 3
    MAX_BACKOFF_TIME = 16.0

    try:
        for attempt in range(retries):
            try:
                response = await vlm_provider.text_chat(
                    prompt=prompt, image_urls=[temp_path]
                )

                if getattr(response, "result_chain", None) is None:
                    raise ValueError("提供方API返回数据结构无效，未包含消息链。")

                result_text = response.result_chain.get_plain_text().strip()
                json_blocks = _extract_json_objects(result_text)
                parsed_index = None

                for block in reversed(json_blocks):
                    try:
                        data = json.loads(block)
                        if "best_index" in data:
                            parsed_index = int(data["best_index"])
                            break
                    except json.JSONDecodeError:
                        continue

                if parsed_index is not None:
                    if parsed_index == 0:
                        return -1
                    if 1 <= parsed_index <= total_count:
                        return parsed_index - 1
                    raise ValueError(
                        f"序列化提取的索引值 {parsed_index} 不在合法区间 [0, {total_count}] 内。"
                    )

                fallback_matches = list(
                    re.finditer(
                        r'(?:"|\')?best_index(?:"|\')?\s*:\s*(\d+)',
                        result_text,
                        re.IGNORECASE,
                    )
                )
                if fallback_matches:
                    index = int(fallback_matches[-1].group(1))
                    if index == 0:
                        return -1
                    if 1 <= index <= total_count:
                        return index - 1
                    raise ValueError(
                        f"正则表达式回退提取的索引值 {index} 不在合法区间 [0, {total_count}] 内。"
                    )
                else:
                    raise ValueError("输出响应未包含约定的特征键结构。")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                err_msg = str(e).lower()
                if any(
                    k in err_msg
                    for k in ["api key", "unauthorized", "blocked", "safety", "quota"]
                ):
                    logger.error(f"遭遇服务方拒绝服务响应，终止重试: {e}")
                    break
                logger.warning(f"VLM评估执行异常 (第 {attempt + 1}/{retries} 次): {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(
                        min(2**attempt, MAX_BACKOFF_TIME) + random.uniform(0, 1)
                    )
    finally:
        try:
            if await asyncio.to_thread(os.path.exists, temp_path):
                await asyncio.to_thread(os.remove, temp_path)
        except Exception as cleanup_err:
            logger.debug(f"清理临时文件失败 (可忽略): {cleanup_err}")

    # 🌟 修复状态混淆：异常耗尽返回 -2，彻底和 "选中第1张图(0)" 脱钩
    logger.error("超出最大重试限制，状态降级返回异常错误码(-2)。")
    return -2
