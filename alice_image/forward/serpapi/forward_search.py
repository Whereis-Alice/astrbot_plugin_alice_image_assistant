"""文字搜图：SerpApi(google_images) 抓候选图直链 + VLM 分批淘汰赛选出最佳图。"""

from __future__ import annotations

import asyncio
import math
import random

from astrbot.api import logger
from astrbot.api.provider import Provider

from .composer import create_collage
from .image_utils import HttpService
from .serpapi_client import SerpApiClient
from .vlm import select_from_collage

# google_images 每页约 100 条，最多翻几页防止失控
_MAX_PAGES = 10
# 单轮内并发处理的批次上限（每批含下载+VLM 调用），避免一次发起过多请求
_MAX_CONCURRENT_BATCHES = 8


async def fetch_image_urls(
    client: SerpApiClient,
    query: str,
    count: int,
    hl: str,
    gl: str,
) -> list[str]:
    """通过 SerpApi google_images 抓取候选图片直链（去重）。hl/gl 由调用方按面板配置传入。"""
    urls: list[str] = []
    seen: set[str] = set()
    page = 0

    while len(urls) < count and page < _MAX_PAGES:
        params: dict = {"q": query, "hl": hl, "gl": gl}
        if page > 0:
            params["ijn"] = page  # google_images 翻页参数（0-based）

        data = await client.get("google_images", params)
        items = data.get("images_results") or data.get("images_result") or []
        if not items:
            break

        new_count = 0
        for it in items:
            url = it.get("original") or it.get("thumbnail")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
                new_count += 1
                if len(urls) >= count:
                    break
        if new_count == 0:
            break
        page += 1

    logger.info(f"[alice_image_serpapi] google_images 抓取到 {len(urls)} 张候选图")
    return urls[:count]


async def _process_batch(
    batch_urls: list[str],
    prompt: str,
    vlm_provider: Provider,
    sem: asyncio.Semaphore,
    http: HttpService,
    label: str = "",
    max_selection: int | None = None,
) -> list[str]:
    """处理单个批次：拼网格图 → VLM 选优 → 返回选中的原始 URL（受 sem 限流）。"""
    async with sem:
        collage_bytes, successful_urls = await create_collage(batch_urls, http)
        if not collage_bytes or not successful_urls:
            logger.warning(f"[alice_image_serpapi] {label}：下载/拼接失败，跳过")
            return []
        selected_indices = await select_from_collage(
            collage_bytes,
            prompt,
            len(successful_urls),
            vlm_provider,
            max_selection,
        )
    winners: list[str] = []
    for index in selected_indices:
        actual_index = index - 1
        if 0 <= actual_index < len(successful_urls):
            winners.append(successful_urls[actual_index])
    logger.info(
        f"[alice_image_serpapi] {label}：{len(successful_urls)} 张 → 选出 {len(winners)} 张"
    )
    return winners


async def run_tournament(
    image_urls: list[str],
    query: str,
    vlm_provider: Provider,
    http: HttpService,
    batch_size: int = 16,
) -> str | None:
    """淘汰赛式筛选：分批拼图 → VLM 选优 → 反复直到剩 1 张。

    包含决赛圈（候选 ≤ 一批时只取一半）与僵局（连续无淘汰则强化提示，再不行随机定胜）逻辑。
    """
    current_winners = image_urls
    round_num = 1
    stalemate_counter = 0

    while len(current_winners) > 1:
        num_batches = (len(current_winners) + batch_size - 1) // batch_size
        logger.info(
            f"[alice_image_serpapi] 第 {round_num} 轮筛选：{len(current_winners)} 张候选，"
            f"分 {num_batches} 批（每批最多 {batch_size} 张）"
        )
        next_round_winners: list[str] = []

        effective_prompt = query
        enhancements: list[str] = []
        # 候选可装进单批时进入决赛圈，做更严格对比；"砍一半"由 batch_max_selection 强约束。
        batch_max_selection: int | None = None
        if len(current_winners) <= batch_size:
            batch_max_selection = max(1, math.ceil(len(current_winners) / 2))
            enhancements.append(
                "重要指示：你正处于决赛圈，请对以下图片进行严格比较，"
                "只保留其中最符合描述的部分作为优胜者。"
            )
        if stalemate_counter > 0:
            enhancements.append(
                "重要指示: 你必须进行筛选。请从以上图片中，严格挑选出一张或几张最符合描述的图片。"
                "如果所有图片都符合，请只选择最优秀的一张。"
            )
        if enhancements:
            effective_prompt = f"{query}\n\n{' '.join(enhancements)}"

        # 同一轮内各批次相互独立，并发处理（下载 + VLM）以避免串行累积超时
        sem = asyncio.Semaphore(_MAX_CONCURRENT_BATCHES)
        batches = [
            current_winners[i : i + batch_size]
            for i in range(0, len(current_winners), batch_size)
        ]
        total_batches = len(batches)
        batch_results = await asyncio.gather(
            *(
                _process_batch(
                    batch,
                    effective_prompt,
                    vlm_provider,
                    sem,
                    http,
                    label=f"第 {round_num} 轮·第 {i}/{total_batches} 批",
                    max_selection=batch_max_selection,
                )
                for i, batch in enumerate(batches, 1)
                if batch
            ),
            return_exceptions=True,
        )
        for res in batch_results:
            if isinstance(res, BaseException):
                logger.warning(
                    f"[alice_image_serpapi] 第 {round_num} 轮某批次异常: {res}"
                )
                continue
            next_round_winners.extend(res)

        if not next_round_winners:
            logger.error("[alice_image_serpapi] 本轮没有产生任何优胜者")
            return None

        previous_count = len(current_winners)
        # 保序去重（避免 set 的哈希乱序导致平票结果不可复现）
        current_winners = list(dict.fromkeys(next_round_winners))
        logger.info(
            f"[alice_image_serpapi] 第 {round_num} 轮结束：{previous_count} → {len(current_winners)} 张"
        )

        if len(current_winners) == previous_count and len(current_winners) > 1:
            stalemate_counter += 1
            logger.warning(
                f"[alice_image_serpapi] 检测到僵局，计数 {stalemate_counter}"
            )
        else:
            stalemate_counter = 0

        if stalemate_counter >= 2:
            logger.warning("[alice_image_serpapi] 僵局持续，随机选出一张作为最终结果")
            current_winners = [random.choice(current_winners)]
            break

        round_num += 1

    return current_winners[0] if current_winners else None
