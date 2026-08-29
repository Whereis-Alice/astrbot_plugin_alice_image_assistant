"""SerpApi 文字搜图服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ..final_verify import FINAL_VERIFY_MAX_EDGE, FinalVerdict, verify_candidate
from ..review import ReviewStatus
from ..session_review import SessionReviewResolver
from .composer import download_image
from .forward_search import fetch_image_urls, run_tournament
from .image_utils import HttpService
from .serpapi_client import SerpApiClient, SerpApiError
from .vlm import VlmReviewError

# 决赛圈默认置信度阈值：与 soutu 侧保持一致，低于该值说明模型只是「大致觉得像」。
DEFAULT_CONFIDENCE_THRESHOLD = 0.6
# 冠军被终选复核否决后，最多再换用几个次优决赛圈候选。
DEFAULT_FINAL_VERIFY_RETRY_LIMIT = 2


@dataclass(slots=True)
class SerpForwardResult:
    image_bytes: bytes | None = None
    image_url: str = ""
    error: str = ""
    review_fallback: bool = False
    review_status: ReviewStatus = ReviewStatus.NOT_RUN
    review_confidence: float | None = None
    review_reason: str = ""


class SerpApiForwardService:
    """抓取 Google 图片候选，并可用视觉模型做淘汰赛。"""

    def __init__(
        self,
        context: Context,
        config: dict[str, Any] | None = None,
        review_resolver: SessionReviewResolver | None = None,
    ) -> None:
        self.context = context
        self.config = config or {}
        self.review_config: dict[str, Any] = {}
        self.review_resolver = review_resolver or SessionReviewResolver(context)
        self.http = HttpService(
            proxy_url=str(self.config.get("proxy_url") or ""),
            user_agent=str(self.config.get("user_agent") or ""),
            allow_image_upload=False,
        )
        keys = self.config.get("serpapi_keys", [])
        if isinstance(keys, str):
            keys = [keys]
        if not isinstance(keys, list):
            keys = []
        self.client = SerpApiClient(keys, self.http)
        self.vlm_provider_id = str(self.config.get("vlm_provider_id") or "").strip()
        self.batch_size = self._bounded_int("batch_size", 16, 2, 64)
        self.scrape_count = self._bounded_int("scrape_count", 16, 1, 200)
        self.gl = str(self.config.get("gl") or "us").strip()
        self.hl = str(self.config.get("hl") or "zh-cn").strip()

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def configure_review(self, review_config: dict[str, Any] | None) -> None:
        """由编排层注入 find_image.llm_review 配置（终选复核开关、阈值、上限）。"""
        self.review_config = review_config if isinstance(review_config, dict) else {}

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

    async def _final_verify_finalists(
        self,
        provider: object,
        winner: str,
        finalists: list[str],
        description: str,
    ) -> tuple[str, ReviewStatus, FinalVerdict | None]:
        """对决赛圈候选做单图全分辨率复核，返回 (选定直链, 审核状态, 判定)。

        淘汰赛全程只在 4x4 网格缩略图上比较，冠军常是「氛围最像」而非「要素全中」；
        用原分辨率单图复核冠军并在被否决时顺位换用次优候选，可显著降低误发率。
        """
        threshold = self._confidence_threshold()
        max_edge = self._bounded_review_int(
            "final_verify_max_edge", FINAL_VERIFY_MAX_EDGE, 640, 2048
        )
        retry_limit = self._bounded_review_int(
            "final_verify_retry_limit", DEFAULT_FINAL_VERIFY_RETRY_LIMIT, 0, 5
        )
        ordered = [winner] + [url for url in finalists if url != winner]
        ordered = ordered[: retry_limit + 1]
        cache: dict[str, bytes | None] = {}
        last_verdict: FinalVerdict | None = None

        for url in ordered:
            if url not in cache:
                cache[url] = await download_image(url, self.http)
            image_bytes = cache[url]
            if not image_bytes:
                logger.warning("[AliceImageSerpApi] 终选复核候选下载失败，跳过：%s", url)
                continue
            verdict = await verify_candidate(
                provider,
                image_bytes,
                description,
                max_edge=max_edge,
                retries=1,
                log_prefix="AliceImageSerpApi",
            )
            last_verdict = verdict
            if verdict.errored:
                # 复核链路自身出错时保留冠军，交由上层 review_fail_open 决定，不丢可用图。
                logger.warning(
                    "[AliceImageSerpApi] 终选复核失败，保留淘汰赛冠军：%s", verdict.error
                )
                return url, ReviewStatus.ERROR, verdict
            if verdict.accepted(threshold):
                logger.info(
                    "[AliceImageSerpApi] 终选复核通过（confidence=%s）：%s",
                    verdict.confidence,
                    verdict.reason or "(无理由)",
                )
                return url, ReviewStatus.MATCHED, verdict
            logger.info(
                "[AliceImageSerpApi] 终选复核否决候选（match=%s confidence=%s）：%s",
                verdict.match,
                verdict.confidence,
                verdict.reason or "(无理由)",
            )

        return winner, ReviewStatus.NO_MATCH, last_verdict

    async def close(self) -> None:
        await self.http.close()

    def available(self) -> bool:
        return self.client.has_keys()

    async def _get_vlm_provider(
        self,
        event: AstrMessageEvent,
        agent_run_context: object | None = None,
    ):
        return await self.review_resolver.resolve(
            event,
            self.vlm_provider_id,
            agent_run_context=agent_run_context,
            log_prefix="AliceImageSerpApi",
        )

    async def search(
        self,
        event: AstrMessageEvent,
        query: str,
        description: str = "",
        review_enabled: bool = True,
        strict_match_enabled: bool = True,
        agent_run_context: object | None = None,
    ) -> SerpForwardResult:
        query = str(query or "").strip()
        if not query:
            return SerpForwardResult(error="搜索关键词不能为空。")
        if not self.available():
            return SerpForwardResult(error="未配置可用的 SerpApi Key。")

        try:
            urls = await fetch_image_urls(
                self.client,
                query,
                self.scrape_count,
                self.hl,
                self.gl,
            )
        except SerpApiError as exc:
            return SerpForwardResult(error=f"SerpApi 搜索失败：{exc}")
        except Exception as exc:
            logger.error("[AliceImageSerpApi] 抓取失败: %s", exc, exc_info=True)
            return SerpForwardResult(error=f"SerpApi 搜索失败：{exc}")

        if not urls:
            return SerpForwardResult(error=f"没有找到与「{query}」相关的图片。")

        selected_url = urls[0]
        review_fallback = False
        review_status = ReviewStatus.NOT_RUN
        review_confidence: float | None = None
        review_reason = ""
        # 判定是否匹配永远用用户原始描述，不用改写后的检索式，避免把改写偏差当成真值。
        eval_desc = description.strip() or query
        if review_enabled:
            provider = await self._get_vlm_provider(event, agent_run_context)
            if provider is None:
                review_fallback = True
                review_status = ReviewStatus.ERROR
            else:
                try:
                    finalists: list[str] = []
                    winner = await run_tournament(
                        urls,
                        eval_desc,
                        provider,
                        self.http,
                        self.batch_size,
                        finalists_out=finalists,
                    )
                    if winner:
                        selected_url = winner
                        review_status = ReviewStatus.MATCHED
                        if self._final_verify_enabled():
                            (
                                selected_url,
                                review_status,
                                verdict,
                            ) = await self._final_verify_finalists(
                                provider,
                                winner,
                                finalists,
                                eval_desc,
                            )
                            if verdict is not None:
                                review_confidence = verdict.confidence
                                review_reason = verdict.reason
                            if review_status is not ReviewStatus.MATCHED:
                                review_fallback = True
                    else:
                        review_fallback = True
                        review_status = ReviewStatus.NO_MATCH
                except VlmReviewError as exc:
                    logger.warning("[AliceImageSerpApi] 审核不可用，保留首图候选: %s", exc)
                    review_fallback = True
                    review_status = ReviewStatus.ERROR
                except Exception as exc:
                    logger.warning("[AliceImageSerpApi] 审核异常，保留首图候选: %s", exc)
                    review_fallback = True
                    review_status = ReviewStatus.ERROR

        if review_status is ReviewStatus.NO_MATCH and strict_match_enabled:
            return SerpForwardResult(
                error="视觉审核已检查 SerpApi 候选，均与描述不匹配。",
                review_fallback=True,
                review_status=review_status,
                review_confidence=review_confidence,
                review_reason=review_reason,
            )

        image_bytes = await download_image(selected_url, self.http)
        if not image_bytes:
            return SerpForwardResult(
                image_url=selected_url,
                error="已选出候选图，但下载失败。",
                review_fallback=review_fallback,
                review_status=review_status,
                review_confidence=review_confidence,
                review_reason=review_reason,
            )
        return SerpForwardResult(
            image_bytes=image_bytes,
            image_url=selected_url,
            review_fallback=review_fallback,
            review_status=review_status,
            review_confidence=review_confidence,
            review_reason=review_reason,
        )
