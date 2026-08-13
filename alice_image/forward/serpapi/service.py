"""SerpApi ???????"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ..review import ReviewStatus
from .composer import download_image
from .forward_search import fetch_image_urls, run_tournament
from .image_utils import HttpService
from .serpapi_client import SerpApiClient, SerpApiError
from .vlm import VlmReviewError


@dataclass(slots=True)
class SerpForwardResult:
    image_bytes: bytes | None = None
    image_url: str = ""
    error: str = ""
    review_fallback: bool = False
    review_status: ReviewStatus = ReviewStatus.NOT_RUN


class SerpApiForwardService:
    """?? Google ?????????????????"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        self.context = context
        self.config = config or {}
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

    async def close(self) -> None:
        await self.http.close()

    def available(self) -> bool:
        return self.client.has_keys()

    async def _get_vlm_provider(self, event: AstrMessageEvent):
        if self.vlm_provider_id:
            provider = self.context.get_provider_by_id(self.vlm_provider_id)
            if provider:
                return provider
            logger.warning(
                "[AliceImageSerpApi] ??????? %s?????????",
                self.vlm_provider_id,
            )
        provider_id = await self.context.get_current_chat_provider_id(
            event.unified_msg_origin
        )
        return self.context.get_provider_by_id(provider_id) if provider_id else None

    async def search(
        self,
        event: AstrMessageEvent,
        query: str,
        description: str = "",
        review_enabled: bool = True,
        strict_match_enabled: bool = True,
    ) -> SerpForwardResult:
        query = str(query or "").strip()
        if not query:
            return SerpForwardResult(error="??????????")
        if not self.available():
            return SerpForwardResult(error="?????? SerpApi Key?")

        try:
            urls = await fetch_image_urls(
                self.client,
                query,
                self.scrape_count,
                self.hl,
                self.gl,
            )
        except SerpApiError as exc:
            return SerpForwardResult(error=f"SerpApi ?????{exc}")
        except Exception as exc:  # noqa: BLE001
            logger.error("[AliceImageSerpApi] ????: %s", exc, exc_info=True)
            return SerpForwardResult(error=f"SerpApi ?????{exc}")

        if not urls:
            return SerpForwardResult(error=f"??????{query}???????")

        selected_url = urls[0]
        review_fallback = False
        review_status = ReviewStatus.NOT_RUN
        if review_enabled:
            provider = await self._get_vlm_provider(event)
            if provider is None:
                review_fallback = True
                review_status = ReviewStatus.ERROR
            else:
                try:
                    winner = await run_tournament(
                        urls,
                        description.strip() or query,
                        provider,
                        self.http,
                        self.batch_size,
                    )
                    if winner:
                        selected_url = winner
                        review_status = ReviewStatus.MATCHED
                    else:
                        review_fallback = True
                        review_status = ReviewStatus.NO_MATCH
                except VlmReviewError as exc:
                    logger.warning("[AliceImageSerpApi] ????????????: %s", exc)
                    review_fallback = True
                    review_status = ReviewStatus.ERROR
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[AliceImageSerpApi] ???????????: %s", exc)
                    review_fallback = True
                    review_status = ReviewStatus.ERROR

        if review_status is ReviewStatus.NO_MATCH and strict_match_enabled:
            return SerpForwardResult(
                error="??????? SerpApi ???????????",
                review_fallback=True,
                review_status=review_status,
            )

        image_bytes = await download_image(selected_url, self.http)
        if not image_bytes:
            return SerpForwardResult(
                image_url=selected_url,
                error="?????????????",
                review_fallback=review_fallback,
                review_status=review_status,
            )
        return SerpForwardResult(
            image_bytes=image_bytes,
            image_url=selected_url,
            review_fallback=review_fallback,
            review_status=review_status,
        )
