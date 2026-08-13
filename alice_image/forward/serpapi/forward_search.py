"""?????SerpApi(google_images) ?????? + VLM ???????????"""

from __future__ import annotations

import asyncio
import math
from astrbot.api import logger
from astrbot.api.provider import Provider

from .composer import create_collage
from .image_utils import HttpService
from .serpapi_client import SerpApiClient
from .vlm import VlmReviewError, select_from_collage

# google_images ??? 100 ???????????
_MAX_PAGES = 10
# ??????????????????+VLM ??????????????
_MAX_CONCURRENT_BATCHES = 8


async def fetch_image_urls(
    client: SerpApiClient,
    query: str,
    count: int,
    hl: str,
    gl: str,
) -> list[str]:
    """?? SerpApi google_images ?????????????hl/gl ????????????"""
    urls: list[str] = []
    seen: set[str] = set()
    page = 0

    while len(urls) < count and page < _MAX_PAGES:
        params: dict = {"q": query, "hl": hl, "gl": gl}
        if page > 0:
            params["ijn"] = page  # google_images ?????0-based?

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

    logger.info(f"[alice_image_serpapi] google_images ??? {len(urls)} ????")
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
    """??????????? ? VLM ?? ? ??????? URL?? sem ????"""
    async with sem:
        collage_bytes, successful_urls = await create_collage(batch_urls, http)
        if not collage_bytes or not successful_urls:
            raise VlmReviewError(f"{label} ????????????")
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
        f"[alice_image_serpapi] {label}?{len(successful_urls)} ? ? ?? {len(winners)} ?"
    )
    return winners


async def run_tournament(
    image_urls: list[str],
    query: str,
    vlm_provider: Provider,
    http: HttpService,
    batch_size: int = 16,
) -> str | None:
    """??????????? ? VLM ?? ? ????? 1 ??

    ???????? ? ??????????????????????????????????
    """
    current_winners = image_urls
    if len(current_winners) == 1:
        winners = await _process_batch(
            current_winners,
            query,
            vlm_provider,
            asyncio.Semaphore(1),
            http,
            label="????",
            max_selection=1,
        )
        return winners[0] if winners else None

    round_num = 1
    stalemate_counter = 0

    while len(current_winners) > 1:
        num_batches = (len(current_winners) + batch_size - 1) // batch_size
        logger.info(
            f"[alice_image_serpapi] ? {round_num} ????{len(current_winners)} ????"
            f"? {num_batches} ?????? {batch_size} ??"
        )
        next_round_winners: list[str] = []

        effective_prompt = query
        enhancements: list[str] = []
        # ?????????????????????"???"? batch_max_selection ????
        batch_max_selection: int | None = None
        if len(current_winners) <= batch_size:
            batch_max_selection = max(1, math.ceil(len(current_winners) / 2))
            enhancements.append(
                "??????????????????????????"
                "???????????????????"
            )
        if stalemate_counter > 0:
            enhancements.append(
                "????: ???????????????????????????????????"
                "?????????????????????"
            )
        if enhancements:
            effective_prompt = f"{query}\n\n{' '.join(enhancements)}"

        # ??????????????????? + VLM??????????
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
                    label=f"? {round_num} ??? {i}/{total_batches} ?",
                    max_selection=batch_max_selection,
                )
                for i, batch in enumerate(batches, 1)
                if batch
            ),
            return_exceptions=True,
        )
        batch_errors: list[str] = []
        for res in batch_results:
            if isinstance(res, BaseException):
                logger.warning(
                    f"[alice_image_serpapi] ? {round_num} ??????: {res}"
                )
                batch_errors.append(str(res))
                continue
            next_round_winners.extend(res)

        if not next_round_winners:
            if batch_errors:
                raise VlmReviewError(
                    f"? {round_num} ????????{batch_errors[0]}"
                )
            logger.error("[alice_image_serpapi] ???????????")
            return None

        previous_count = len(current_winners)
        # ??????? set ????????????????
        current_winners = list(dict.fromkeys(next_round_winners))
        logger.info(
            f"[alice_image_serpapi] ? {round_num} ????{previous_count} ? {len(current_winners)} ?"
        )

        if len(current_winners) == previous_count and len(current_winners) > 1:
            stalemate_counter += 1
            logger.warning(
                f"[alice_image_serpapi] ???????? {stalemate_counter}"
            )
        else:
            stalemate_counter = 0

        if stalemate_counter >= 2:
            raise VlmReviewError("?????????????????")

        round_num += 1

    return current_winners[0] if current_winners else None
