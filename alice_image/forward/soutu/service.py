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

from ..final_verify import FINAL_VERIFY_MAX_EDGE, verify_candidate
from ..imagehash import DuplicateFilter, ImageFingerprint, fingerprint
from ..review import ReviewStatus
from ..session_review import SessionReviewResolver
from .composer import ComposerManager
from .scraper import ScraperManager
from .vlm import INDEX_ERROR, INDEX_NO_MATCH, select_best_image_detailed

JPEG_QUALITY = 85
# 拼图阶段默认置信度阈值：低于该值说明模型只是"大致觉得像"，缩略图上这种判断错得最多。
DEFAULT_CONFIDENCE_THRESHOLD = 0.6
# 单轮内允许因置信度不足 / 复核不通过而剔除候选并重选的次数上限。
DEFAULT_FINAL_VERIFY_RETRY_LIMIT = 2
DEFAULT_VLM_MAX_CONCURRENCY = 2
# 近似重复的默认汉明距离阈值：同一张图的不同压缩 / 水印版本通常相差 5 位以内。
DEFAULT_DEDUP_HAMMING_DISTANCE = 5


@dataclass(slots=True)
class SoutuForwardResult:
    image_bytes: bytes | None = None
    image_url: str = ""
    error: str = ""
    review_fallback: bool = False
    review_status: ReviewStatus = ReviewStatus.NOT_RUN
    reviewed_count: int = 0
    review_confidence: float | None = None
    review_reason: str = ""

    def __iter__(self):
        """Keep compatibility with the pre-1.4 tuple return contract."""
        yield self.image_bytes
        yield self.error
        yield self.review_fallback


@dataclass(slots=True)
class SoutuSelection:
    """一轮视觉筛选（拼图选图 + 终选复核）的结果。

    保留 4 元组解包能力，兼容历史调用方与既有测试替身。
    """

    status: ReviewStatus = ReviewStatus.NOT_RUN
    image_url: str = ""
    image_bytes: bytes = b""
    error: str = ""
    confidence: float | None = None
    reason: str = ""

    def __iter__(self):
        yield self.status
        yield self.image_url
        yield self.image_bytes
        yield self.error


def coerce_selection(value: object) -> SoutuSelection:
    """把 _vlm_selection 的返回值统一成 SoutuSelection。

    历史实现（以及测试替身）返回 (status, url, bytes, error) 四元组，必须继续支持。
    """
    if isinstance(value, SoutuSelection):
        return value
    if isinstance(value, tuple):
        parts = list(value) + [None] * (6 - len(value))
        return SoutuSelection(
            status=parts[0] if parts[0] is not None else ReviewStatus.NOT_RUN,
            image_url=str(parts[1] or ""),
            image_bytes=parts[2] or b"",
            error=str(parts[3] or ""),
            confidence=parts[4],
            reason=str(parts[5] or ""),
        )
    return SoutuSelection(
        status=getattr(value, "status", ReviewStatus.NOT_RUN),
        image_url=str(getattr(value, "image_url", "") or ""),
        image_bytes=getattr(value, "image_bytes", b"") or b"",
        error=str(getattr(value, "error", "") or ""),
        confidence=getattr(value, "confidence", None),
        reason=str(getattr(value, "reason", "") or ""),
    )


class SoutuSearchService:
    def __init__(
        self,
        context: Context,
        config: dict | None = None,
        review_resolver: SessionReviewResolver | None = None,
    ):
        self.context = context
        self.config = config or {}
        self.review_config: dict = {}
        self.review_resolver = review_resolver or SessionReviewResolver(context)
        self.scraper_mgr = ScraperManager()
        self.composer_mgr = ComposerManager()
        self._vlm_semaphore: asyncio.Semaphore | None = None

    def configure_review(self, review_config: dict | None) -> None:
        """由编排层注入 find_image.llm_review 配置（并发上限 / 终选复核开关与阈值）。"""
        self.review_config = review_config if isinstance(review_config, dict) else {}
        # 并发上限可能随配置变化，重置信号量以便下次调用按新值重建。
        self._vlm_semaphore = None

    async def terminate(self) -> None:
        await self.scraper_mgr.close_all()
        await self.composer_mgr.close_all()
        logger.info("SouTuShenQi 插件资源回收完成。")

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _bounded_review_int(
        self, key: str, default: int, minimum: int, maximum: int
    ) -> int:
        try:
            value = int(self.review_config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _confidence_threshold(self) -> float:
        """终选置信度阈值，收敛到 0..1。"""
        try:
            value = float(
                self.review_config.get(
                    "confidence_threshold", DEFAULT_CONFIDENCE_THRESHOLD
                )
            )
        except (TypeError, ValueError):
            value = DEFAULT_CONFIDENCE_THRESHOLD
        return max(0.0, min(1.0, value))

    def _final_verify_enabled(self) -> bool:
        return bool(self.review_config.get("final_verify_enabled", True))

    def _semaphore(self) -> asyncio.Semaphore:
        """懒加载视觉模型并发信号量，取值来自 find_image.llm_review.max_concurrency。"""
        if self._vlm_semaphore is None:
            limit = self._bounded_review_int(
                "max_concurrency", DEFAULT_VLM_MAX_CONCURRENCY, 1, 8
            )
            self._vlm_semaphore = asyncio.Semaphore(limit)
        return self._vlm_semaphore

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

    def _fingerprint_sync(
        self, img_bytes: bytes, min_res: int
    ) -> ImageFingerprint:
        """一次解码完成分辨率校验与 aHash/dHash 计算（供线程池调用）。"""
        result = fingerprint(img_bytes, min_resolution=min_res)
        if not result.valid and result.width and result.height:
            logger.debug(
                "搜图神器候选图分辨率不足（%sx%s < %s），已跳过。",
                result.width,
                result.height,
                min_res,
            )
        elif not result.valid:
            logger.warning("搜图神器候选图无法解码，已跳过该候选。")
        return result

    async def _download_valid_batch(
        self,
        url_pool: list[str],
        target_count: int,
        min_resolution: int,
        dedup: DuplicateFilter,
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
                item = await loop.run_in_executor(
                    None,
                    self._fingerprint_sync,
                    img_bytes,
                    min_resolution,
                )
                # 近似重复（同图不同压缩/水印/尺寸）不再占用视觉模型的评估名额，
                # 等量预算可以看到更多"真正不同"的候选，命中率随之提升。
                if item.valid and dedup.add_if_new(item):
                    valid_items.append((url, img_bytes))
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

    async def _select_on_collage(
        self,
        provider: Provider,
        pool: list[tuple[str, bytes]],
        eval_desc: str,
    ) -> tuple[SoutuSelection | None, list[tuple[str, bytes]], int, float | None, str]:
        """在拼图上选一张候选，返回 (提前结束的结论, 有效候选, 下标, 置信度, 理由)。"""
        collage_bytes, valid_items = await self.composer_mgr.create_collage_from_items(
            pool
        )
        if not collage_bytes or not valid_items:
            return (
                SoutuSelection(
                    ReviewStatus.ERROR, "", b"", "图像组合处理失败，候选数据损坏。"
                ),
                [],
                -1,
                None,
                "",
            )

        async with self._semaphore():
            selection = await select_best_image_detailed(
                provider, collage_bytes, eval_desc, len(valid_items)
            )

        if selection.index == INDEX_NO_MATCH:
            # -1 是模型的明确判定（全不匹配），必须交给 strict_match_enabled 决定去留。
            return (
                SoutuSelection(
                    ReviewStatus.NO_MATCH,
                    "",
                    b"",
                    "",
                    selection.confidence,
                    selection.reason,
                ),
                valid_items,
                -1,
                selection.confidence,
                selection.reason,
            )
        if selection.index == INDEX_ERROR or not 0 <= selection.index < len(valid_items):
            # -2 与越界都属于技术错误，保留首图候选交给 review_fail_open 决定。
            first_url, first_bytes = valid_items[0]
            return (
                SoutuSelection(
                    ReviewStatus.ERROR,
                    first_url,
                    first_bytes,
                    "视觉审核模型调用或解析失败。",
                    selection.confidence,
                    selection.reason,
                ),
                valid_items,
                -1,
                selection.confidence,
                selection.reason,
            )
        return None, valid_items, selection.index, selection.confidence, selection.reason

    async def _vlm_selection(
        self,
        provider: Provider,
        items: list[tuple[str, bytes]],
        eval_desc: str,
    ) -> SoutuSelection:
        """拼图选图 + 终选单图全分辨率复核。

        拼图只有 300x300 缩略图，细节几乎全丢；把拼图选中的那一张按原分辨率再送一次
        视觉模型做结构化判定（match / confidence / reason），能把"看着像但细节不符"
        的候选挡在发送之前，这是本次精准度改造中收益最大的一环。
        """
        threshold = self._confidence_threshold()
        final_verify = self._final_verify_enabled()
        max_edge = self._bounded_review_int(
            "final_verify_max_edge", FINAL_VERIFY_MAX_EDGE, 640, 2048
        )
        retry_limit = self._bounded_review_int(
            "final_verify_retry_limit", DEFAULT_FINAL_VERIFY_RETRY_LIMIT, 0, 5
        )
        pool = list(items)
        rejections = 0
        last_confidence: float | None = None
        last_reason = ""

        while pool:
            early, valid_items, index, confidence, reason = await self._select_on_collage(
                provider, pool, eval_desc
            )
            if early is not None:
                return early
            last_confidence, last_reason = confidence, reason
            candidate_url, candidate_bytes = valid_items[index]
            remaining = [
                item for position, item in enumerate(valid_items) if position != index
            ]

            if confidence is not None and confidence < threshold:
                # 拼图阶段自评置信度过低，直接剔除重选，避免把"勉强选的"送进复核甚至发出去。
                logger.info(
                    "搜图神器拼图选图置信度 %.2f 低于阈值 %.2f，剔除该候选后重选：%s",
                    confidence,
                    threshold,
                    reason or "(无理由)",
                )
                rejections += 1
                if rejections > retry_limit:
                    break
                pool = remaining
                continue

            if not final_verify:
                return SoutuSelection(
                    ReviewStatus.MATCHED,
                    candidate_url,
                    candidate_bytes,
                    "",
                    confidence,
                    reason,
                )

            verdict = await verify_candidate(
                provider,
                candidate_bytes,
                eval_desc,
                max_edge=max_edge,
                retries=1,
                log_prefix="AliceImageSoutu",
            )
            if verdict.errored:
                # 复核链路本身出错时保留候选，交由 review_fail_open 决定，绝不因此丢掉可用图。
                logger.warning(
                    "搜图神器终选复核失败，保留拼图候选等待 fail_open 决策：%s",
                    verdict.error,
                )
                return SoutuSelection(
                    ReviewStatus.ERROR,
                    candidate_url,
                    candidate_bytes,
                    f"终选复核失败：{verdict.error}",
                    confidence,
                    reason,
                )
            if verdict.accepted(threshold):
                logger.info(
                    "搜图神器终选复核通过（confidence=%s）：%s",
                    verdict.confidence,
                    verdict.reason or reason or "(无理由)",
                )
                return SoutuSelection(
                    ReviewStatus.MATCHED,
                    candidate_url,
                    candidate_bytes,
                    "",
                    verdict.confidence if verdict.confidence is not None else confidence,
                    verdict.reason or reason,
                )

            logger.info(
                "搜图神器终选复核否决候选（match=%s confidence=%s）：%s",
                verdict.match,
                verdict.confidence,
                verdict.reason or "(无理由)",
            )
            last_confidence = verdict.confidence
            last_reason = verdict.reason or reason
            rejections += 1
            if rejections > retry_limit:
                break
            pool = remaining

        return SoutuSelection(
            ReviewStatus.NO_MATCH, "", b"", "", last_confidence, last_reason
        )

    def _format_image_sync(self, img_bytes: bytes) -> bytes:
        try:
            with io.BytesIO(img_bytes) as img_io, Image.open(img_io) as img:
                if img.format not in ["JPEG", "PNG"]:
                    if img.mode in ("RGBA", "LA") or (
                        img.mode == "P" and "transparency" in img.info
                    ):
                        try:
                            img = img.convert("RGBA")
                            bg = Image.new("RGB", img.size, (255, 255, 255))
                            bg.paste(img, mask=img.split()[-1])
                            img = bg
                        except Exception as exc:
                            logger.warning(
                                "搜图神器透明通道合成失败，退化为直接转 RGB：%s", exc
                            )
                            img = img.convert("RGB")
                    else:
                        img = img.convert("RGB")

                    with io.BytesIO() as buf:
                        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                        return buf.getvalue()
                return img_bytes
        except UnidentifiedImageError:
            logger.warning("搜图神器无法识别图片格式，按原始字节发送。")
            return img_bytes
        except Exception as exc:
            logger.warning("搜图神器图片格式化失败，按原始字节发送：%s", exc)
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
        # 判定图片是否匹配永远使用用户原始描述，绝不用改写后的检索词，
        # 否则会把检索改写引入的偏差当成真值。
        eval_desc = description.strip() or keyword
        batch_size = self._bounded_int("batch_size", 9, 1, 16)
        review_rounds = self._bounded_int("review_rounds", 3, 1, 6)
        min_resolution = self._bounded_int("min_resolution", 500, 100, 4000)
        dedup_distance = self._bounded_int(
            "dedup_hamming_distance", DEFAULT_DEDUP_HAMMING_DISTANCE, 0, 16
        )
        total_target = batch_size * (review_rounds if use_vlm_selection else 1)
        primary_pool, bing_pool, primary_error = await self._collect_url_pools(
            keyword, total_target
        )
        dedup = DuplicateFilter(dedup_distance)
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
                first_pool, batch_size, min_resolution, dedup
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
                        dedup,
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
        last_confidence: float | None = None
        last_reason = ""
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
            selection = coerce_selection(
                await self._vlm_selection(provider, items, eval_desc)
            )
            status = selection.status
            last_confidence = selection.confidence
            last_reason = selection.reason
            if status is ReviewStatus.MATCHED:
                logger.info(
                    "搜图神器第 %s 轮找到匹配图片，累计审核 %s 张候选。",
                    round_index,
                    reviewed_count,
                )
                return SoutuForwardResult(
                    image_bytes=await self._format_image(selection.image_bytes),
                    image_url=selection.image_url,
                    review_status=status,
                    reviewed_count=reviewed_count,
                    review_confidence=selection.confidence,
                    review_reason=selection.reason,
                )
            if status is ReviewStatus.ERROR:
                # 技术错误时优先用本轮选出的候选，缺失则回退首图，保证 fail_open 有图可放行。
                fallback_url = selection.image_url
                fallback_bytes = selection.image_bytes
                if not fallback_bytes:
                    fallback_url, fallback_bytes = items[0]
                logger.warning(
                    "搜图神器视觉审核发生技术错误，保留首图候选等待 fail_open 决策：%s",
                    selection.error,
                )
                return SoutuForwardResult(
                    image_bytes=await self._format_image(fallback_bytes),
                    image_url=fallback_url,
                    error=selection.error,
                    review_fallback=True,
                    review_status=status,
                    reviewed_count=reviewed_count,
                    review_confidence=selection.confidence,
                    review_reason=selection.reason,
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
                    review_confidence=last_confidence,
                    review_reason=last_reason,
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
                review_confidence=last_confidence,
                review_reason=last_reason,
            )

        return SoutuForwardResult(
            error=primary_error or "未找到符合分辨率要求且可访问的图像资源。"
        )
