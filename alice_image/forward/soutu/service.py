# -*- coding: utf-8 -*-
"""搜图神器文字搜图与渐进式视觉筛选。"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import Provider
from astrbot.api.star import Context
from PIL import Image, UnidentifiedImageError

from ..review import ReviewStatus
from ..session_review import SessionReviewResolver
from .composer import ComposerManager
from .scraper import ScraperManager
from .vlm import select_best_image_index

JPEG_QUALITY = 85


@dataclass(slots=True)
class SoutuForwardResult:
    image_bytes: bytes | None = None
    image_url: str = ""
    error: str = ""
    review_fallback: bool = False
    review_status: ReviewStatus = ReviewStatus.NOT_RUN
    reviewed_count: int = 0

    def __iter__(self):
        """Keep compatibility with the pre-1.4 tuple return contract."""
        yield self.image_bytes
        yield self.error
        yield self.review_fallback


class SoutuSearchService:
    def __init__(
        self,
        context: Context,
        config: dict | None = None,
        review_resolver: SessionReviewResolver | None = None,
    ):
        self.context = context
        self.config = config or {}
        self.review_resolver = review_resolver or SessionReviewResolver(context)
        self.scraper_mgr = ScraperManager()
        self.composer_mgr = ComposerManager()
        self._vlm_semaphore = asyncio.Semaphore(2)

    async def terminate(self) -> None:
        await self.scraper_mgr.close_all()
        await self.composer_mgr.close_all()
        logger.info("SouTuShenQi 插件资源回收完成。")

    def _bounded_int(
        self, key: str, default: int, minimum: int, maximum: int
    ) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    async def _get_vlm_provider(
        self,
        event: AstrMessageEvent,
        agent_run_context: object | None = None,
    ) -> Provider | None:
        return await self.review_resolver.resolve(
            event,
            str(self.config.get("vlm_provider_id") or ""),
            agent_run_context=agent_run_context,
            log_prefix="AliceImageSoutu",
        )

    def _validate_and_hash_sync(
        self, img_bytes: bytes, min_res: int
    ) -> tuple[bool, str]:
        try:
            with Image.open(io.BytesIO(img_bytes)) as img:
                if img.width < min_res or img.height < min_res:
                    return False, ""
                img = img.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
                pixels = list(img.getdata())
                avg = sum(pixels) / len(pixels)
                bits = "".join("1" if pixel > avg else "0" for pixel in pixels)
                return True, hex(int(bits, 2))[2:].zfill(16)
        except Exception:
            return False, ""

    async def _download_valid_batch(
        self,
        url_pool: list[str],
        target_count: int,
        min_resolution: int,
        seen_hashes: set[str],
    ) -> list[tuple[str, bytes]]:
        valid_items: list[tuple[str, bytes]] = []
        loop = asyncio.get_running_loop()

        while url_pool and len(valid_items) < target_count:
            needed = target_count - len(valid_items)
            download_count = min(len(url_pool), max(needed, needed * 2))
            batch_urls = url_pool[:download_count]
            del url_pool[:download_count]
            downloaded = await self.composer_mgr.download_image_batch(
                batch_urls, target_count=len(batch_urls)
            )
            for url, img_bytes in downloaded:
                if len(valid_items) >= target_count:
                    break
                is_valid, image_hash = await loop.run_in_executor(
                    None,
                    self._validate_and_hash_sync,
                    img_bytes,
                    min_resolution,
                )
                if is_valid and image_hash not in seen_hashes:
                    valid_items.append((url, img_bytes))
                    seen_hashes.add(image_hash)
        return valid_items

    async def _collect_url_pools(
        self, keyword: str, total_target: int
    ) -> tuple[list[str], list[str], str]:
        primary_urls: list[str] = []
        primary_error = ""
        if self.config.get("primary_source_enabled", True):
            primary_urls, primary_error = await self.scraper_mgr.fetch_image_urls(
                keyword, total_target * 4
            )

        return list(primary_urls), [], primary_error

    async def _ensure_bing_pool(
        self,
        keyword: str,
        total_target: int,
        bing_pool: list[str],
        bing_loaded: bool,
    ) -> bool:
        if bing_loaded or not self.config.get("bing_fallback_enabled", True):
            return bing_loaded
        bing_pool.extend(
            await self.scraper_mgr.fetch_bing_image_urls(keyword, total_target * 3)
        )
        return True

    async def _vlm_selection(
        self,
        provider: Provider,
        items: list[tuple[str, bytes]],
        eval_desc: str,
    ) -> tuple[ReviewStatus, str, bytes, str]:
        collage_bytes, valid_items = await self.composer_mgr.create_collage_from_items(
            items
        )
        if not collage_bytes or not valid_items:
            return ReviewStatus.ERROR, "", b"", "图像组合处理失败，候选数据损坏。"

        async with self._vlm_semaphore:
            best_idx = await select_best_image_index(
                provider, collage_bytes, eval_desc, len(valid_items)
            )

        if best_idx == -1:
            return ReviewStatus.NO_MATCH, "", b"", ""
        if best_idx == -2 or not 0 <= best_idx < len(valid_items):
            return ReviewStatus.ERROR, "", b"", "视觉审核模型调用或解析失败。"

        final_url, final_bytes = valid_items[best_idx]
        return ReviewStatus.MATCHED, final_url, final_bytes, ""

    def _format_image_sync(self, img_bytes: bytes) -> bytes:
        try:
            with io.BytesIO(img_bytes) as img_io:
                with Image.open(img_io) as img:
                    if img.format not in ["JPEG", "PNG"]:
                        if img.mode in ("RGBA", "LA") or (
                            img.mode == "P" and "transparency" in img.info
                        ):
                            try:
                                img = img.convert("RGBA")
                                bg = Image.new("RGB", img.size, (255, 255, 255))
                                bg.paste(img, mask=img.split()[-1])
                                img = bg
                            except Exception:
                                img = img.convert("RGB")
                        else:
                            img = img.convert("RGB")

                        with io.BytesIO() as buf:
                            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                            return buf.getvalue()
                    return img_bytes
        except UnidentifiedImageError:
            return img_bytes
        except Exception:
            return img_bytes

    async def _format_image(self, img_bytes: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._format_image_sync, img_bytes)

    async def search(
        self,
        event: AstrMessageEvent,
        keyword: str,
        description: str = "",
        use_vlm_selection: bool = True,
        strict_match_enabled: bool = True,
        agent_run_context: object | None = None,
    ) -> SoutuForwardResult:
        eval_desc = description.strip() or keyword
        batch_size = self._bounded_int("batch_size", 9, 1, 16)
        review_rounds = self._bounded_int("review_rounds", 3, 1, 6)
        min_resolution = self._bounded_int("min_resolution", 500, 100, 4000)
        total_target = batch_size * (review_rounds if use_vlm_selection else 1)
        primary_pool, bing_pool, primary_error = await self._collect_url_pools(
            keyword, total_target
        )
        seen_hashes: set[str] = set()
        bing_loaded = False

        async def next_batch(prefer_bing: bool = False) -> list[tuple[str, bytes]]:
            nonlocal bing_loaded
            first_pool = primary_pool
            second_pool = bing_pool
            if prefer_bing and self.config.get("bing_fallback_enabled", True):
                bing_loaded = await self._ensure_bing_pool(
                    keyword,
                    total_target,
                    bing_pool,
                    bing_loaded,
                )
                first_pool, second_pool = bing_pool, primary_pool

            items = await self._download_valid_batch(
                first_pool, batch_size, min_resolution, seen_hashes
            )
            if len(items) < batch_size:
                if second_pool is bing_pool:
                    bing_loaded = await self._ensure_bing_pool(
                        keyword,
                        total_target,
                        bing_pool,
                        bing_loaded,
                    )
                items.extend(
                    await self._download_valid_batch(
                        second_pool,
                        batch_size - len(items),
                        min_resolution,
                        seen_hashes,
                    )
                )
            return items

        if not use_vlm_selection:
            items = await next_batch()
            if not items:
                return SoutuForwardResult(
                    error=primary_error
                    or "未找到符合分辨率要求且可访问的图像资源。"
                )
            image_url, image_bytes = items[0]
            return SoutuForwardResult(
                image_bytes=await self._format_image(image_bytes),
                image_url=image_url,
            )

        provider = await self._get_vlm_provider(event, agent_run_context)
        if provider is None:
            items = await next_batch()
            if not items:
                return SoutuForwardResult(
                    error=primary_error
                    or "未找到符合分辨率要求且可访问的图像资源。",
                    review_status=ReviewStatus.ERROR,
                )
            image_url, image_bytes = items[0]
            logger.warning("搜图神器未找到可用视觉模型，保留首图候选等待 fail_open 决策。")
            return SoutuForwardResult(
                image_bytes=await self._format_image(image_bytes),
                image_url=image_url,
                review_fallback=True,
                review_status=ReviewStatus.ERROR,
                reviewed_count=len(items),
            )

        reviewed_count = 0
        first_candidate: tuple[str, bytes] | None = None
        for round_index in range(1, review_rounds + 1):
            items = await next_batch(prefer_bing=round_index > 1)
            if not items:
                break
            if first_candidate is None:
                first_candidate = items[0]

            reviewed_count += len(items)
            logger.info(
                "搜图神器开始第 %s/%s 轮视觉筛选，本轮 %s 张候选。",
                round_index,
                review_rounds,
                len(items),
            )
            status, image_url, image_bytes, error = await self._vlm_selection(
                provider, items, eval_desc
            )
            if status is ReviewStatus.MATCHED:
                logger.info(
                    "搜图神器第 %s 轮找到匹配图片，累计审核 %s 张候选。",
                    round_index,
                    reviewed_count,
                )
                return SoutuForwardResult(
                    image_bytes=await self._format_image(image_bytes),
                    image_url=image_url,
                    review_status=status,
                    reviewed_count=reviewed_count,
                )
            if status is ReviewStatus.ERROR:
                fallback_url, fallback_bytes = items[0]
                logger.warning(
                    "搜图神器视觉审核发生技术错误，保留首图候选等待 fail_open 决策：%s",
                    error,
                )
                return SoutuForwardResult(
                    image_bytes=await self._format_image(fallback_bytes),
                    image_url=fallback_url,
                    error=error,
                    review_fallback=True,
                    review_status=status,
                    reviewed_count=reviewed_count,
                )

            logger.info(
                "搜图神器第 %s 轮候选均不匹配，继续检查后续候选。",
                round_index,
            )

        if reviewed_count:
            if not strict_match_enabled and first_candidate is not None:
                image_url, image_bytes = first_candidate
                logger.warning(
                    "搜图神器严格匹配已关闭，视觉审核无匹配后按配置放行首图候选。"
                )
                return SoutuForwardResult(
                    image_bytes=await self._format_image(image_bytes),
                    image_url=image_url,
                    review_fallback=True,
                    review_status=ReviewStatus.NO_MATCH,
                    reviewed_count=reviewed_count,
                )
            logger.info(
                "搜图神器累计审核 %s 张候选后仍无匹配结果，不发送候选首图。",
                reviewed_count,
            )
            return SoutuForwardResult(
                error=f"视觉审核已检查 {reviewed_count} 张候选，均与描述不匹配。",
                review_fallback=True,
                review_status=ReviewStatus.NO_MATCH,
                reviewed_count=reviewed_count,
            )

        return SoutuForwardResult(
            error=primary_error or "未找到符合分辨率要求且可访问的图像资源。"
        )
