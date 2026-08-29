"""拼图阶段的视觉选图（B 项：返回置信度与理由）。

拼图上每张候选只有缩略图，模型很容易"挑一张最好看的"充数。
让模型同时给出 confidence 与 reason，上层就能用阈值把"勉强选的"挡掉，
再交给单图全分辨率复核确认，这是提升找图准确率最关键的一步。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import aiofiles
from astrbot.api import logger
from astrbot.api.provider import Provider

from ..vlm_json import (
    coerce_confidence,
    coerce_index,
    coerce_reason,
    extract_json_objects,
    parse_json_payload,
)

# 保留旧私有名，避免外部引用（含历史测试）断裂。
_extract_json_objects = extract_json_objects

MAX_BACKOFF_TIME = 16.0
RETRIES = 3
# 解析失败 / 调用耗尽用 -2，明确区别于"全不匹配"的 -1：
# -2 属于技术错误应走 review_fail_open，-1 属于明确判定应走 strict_match_enabled。
INDEX_NO_MATCH = -1
INDEX_ERROR = -2
_FENCE = "\u0060\u0060\u0060"


@dataclass(slots=True)
class CollageSelection:
    """拼图选图结果。

    index 为 0-based 候选下标；-1 表示全不匹配，-2 表示技术错误。
    """

    index: int = INDEX_ERROR
    confidence: float | None = None
    reason: str = ""

    @property
    def matched(self) -> bool:
        """是否选中了某一张候选。"""
        return self.index >= 0


def build_selection_prompt(description: str, total_count: int) -> str:
    """构造拼图选图 prompt。

    强调"只依据看得见的事物""不要因为风格好看就选""宁可返回 0"，
    是因为模型默认倾向于给出一个答案，而错选一张的代价远高于返回 0 后换下一批候选。
    """
    safe_desc = str(description or "")[:300].replace(_FENCE, "")
    return (
        "这是一张由 "
        + str(total_count)
        + " 张候选图片拼成的网格图。每张图片的左上角有一个黑底白字的数字编号，"
        "编号从 1 开始，按从左到右、从上到下的顺序排列。\n\n"
        "用户想要的图片描述：「" + safe_desc + "」\n\n"
        "【判断准则】\n"
        "1. 只依据你在图中真实看得见的事物（主体、人数、发色发型、服饰、动作、场景、"
        "画面类型是照片还是插画）来判断，不要推测画面之外的内容。\n"
        "2. 不要因为某张图片画质好、构图美、风格好看就选它；只看它是否符合描述。\n"
        "3. 描述中的任一关键要素与图片明显冲突，这张图片就不算符合。\n"
        "4. 如果所有图片都不符合描述，请返回 best_index 为 0。"
        "宁可返回 0 让系统换下一批候选，也不要勉强选一张凑数。\n"
        "5. confidence 是 0 到 1 之间的小数，表示你对这次选择的把握程度。"
        "如果关键细节看不清、或只是大致相似，请给 0.3~0.5 的低分，不要虚报高置信度。\n"
        "6. reason 用一句中文说明你在选中的图片里看到了什么。\n\n"
        "【输出格式】只输出一个 JSON 对象，不要 markdown 代码块，不要额外解释：\n"
        '{"best_index": 3, "confidence": 0.78, "reason": "第3张是银发少女穿白色连衣裙站在草地上"}'
    )


def _resolve_index(raw_index: int, total_count: int) -> int:
    """把 1-based 编号转成 0-based 下标；0 表示全不匹配。"""
    if raw_index == 0:
        return INDEX_NO_MATCH
    if 1 <= raw_index <= total_count:
        return raw_index - 1
    raise ValueError(
        "提取的索引值 " + str(raw_index) + " 不在合法区间 [0, " + str(total_count) + "] 内。"
    )


def parse_selection(text: str, total_count: int) -> CollageSelection:
    """宽容解析选图响应；兼容新格式、旧格式（仅 best_index）、裸数字与 markdown 围栏。"""
    data = parse_json_payload(text, required_key="best_index")
    if data is not None:
        raw_index = coerce_index(data.get("best_index"))
        if raw_index is not None:
            return CollageSelection(
                index=_resolve_index(raw_index, total_count),
                confidence=coerce_confidence(data.get("confidence")),
                reason=coerce_reason(data.get("reason")),
            )

    # 兜底 1：正则直接抓 best_index，应对 JSON 被截断或引号错乱的情况。
    fallback_matches = list(
        re.finditer(
            r'(?:"|\')?best_index(?:"|\')?\s*:\s*(\d+)',
            text,
            re.IGNORECASE,
        )
    )
    if fallback_matches:
        raw = int(fallback_matches[-1].group(1))
        return CollageSelection(index=_resolve_index(raw, total_count))

    # 兜底 2：整段响应就是一个裸数字（老 prompt 时代常见）。
    stripped = text.strip()
    if re.fullmatch(r"\d+", stripped):
        return CollageSelection(index=_resolve_index(int(stripped), total_count))

    raise ValueError("输出响应未包含约定的特征键结构。")


async def select_best_image_detailed(
    vlm_provider: Provider,
    image_bytes: bytes,
    description: str,
    total_count: int,
) -> CollageSelection:
    """在拼图上选出最匹配的候选，并带回置信度与理由。"""
    if total_count <= 0:
        return CollageSelection(index=INDEX_NO_MATCH)

    unique_filename = "vlm_soutu_collage_" + uuid.uuid4().hex + ".jpg"
    temp_path = str(Path(tempfile.gettempdir()) / unique_filename)

    try:
        async with aiofiles.open(temp_path, "wb") as f:
            await f.write(image_bytes)
    except Exception as e:
        logger.error(f"写入临时拼图失败: {e}")
        # 修复状态混淆：写入失败属于技术错误，返回 -2 而不是 -1。
        return CollageSelection(index=INDEX_ERROR, reason=f"写入临时拼图失败: {e}")

    prompt = build_selection_prompt(description, total_count)

    try:
        for attempt in range(RETRIES):
            try:
                response = await vlm_provider.text_chat(
                    prompt=prompt, image_urls=[temp_path]
                )

                result_text = str(
                    getattr(response, "completion_text", "") or ""
                ).strip()
                if (
                    not result_text
                    and getattr(response, "result_chain", None) is not None
                ):
                    result_text = str(
                        response.result_chain.get_plain_text() or ""
                    ).strip()
                if not result_text:
                    raise ValueError("视觉审核模型返回空响应。")

                return parse_selection(result_text, total_count)

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
                logger.warning(f"VLM评估执行异常 (第 {attempt + 1}/{RETRIES} 次): {e}")
                if attempt < RETRIES - 1:
                    await asyncio.sleep(
                        min(2**attempt, MAX_BACKOFF_TIME) + random.uniform(0, 1)
                    )
    finally:
        try:
            if await asyncio.to_thread(os.path.exists, temp_path):
                await asyncio.to_thread(os.remove, temp_path)
        except Exception as cleanup_err:
            logger.debug(f"清理临时文件失败 (可忽略): {cleanup_err}")

    logger.error("超出最大重试限制，状态降级返回异常错误码(-2)。")
    return CollageSelection(index=INDEX_ERROR, reason="视觉审核模型调用或解析失败。")


async def select_best_image_index(
    vlm_provider: Provider, image_bytes: bytes, description: str, total_count: int
) -> int:
    """向后兼容包装：只返回 0-based 下标（-1 全不匹配 / -2 技术错误）。"""
    selection = await select_best_image_detailed(
        vlm_provider, image_bytes, description, total_count
    )
    return selection.index
