"""统一文字找图、自动选源与失败回退。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .pixiv_search import PixivForwardSearchService
from .serpapi.service import SerpApiForwardService
from .soutu.service import SoutuSearchService

SOURCE_NAMES = ("pixiv", "soutu", "serpapi")
_PIXIV_HINTS = (
    "pixiv",
    "p站",
    "二次元",
    "动漫",
    "动画",
    "插画",
    "角色",
    "同人",
    "壁纸",
    "立绘",
    "vtuber",
    "vocaloid",
)


@dataclass(slots=True)
class ForwardOutcome:
    success: bool = False
    source: str = ""
    attempted_sources: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    message_sent: bool = False
    review_fallback: bool = False
    pixiv_ids: list[int] = field(default_factory=list)

    def to_json(self) -> str:
        payload = asdict(self)
        payload["instruction"] = (
            "图片已发送，请简短说明使用的来源；不要虚构图片内容。"
            if self.success and self.message_sent
            else "请根据 errors 向用户说明失败原因，并建议调整关键词或配置。"
        )
        return json.dumps(payload, ensure_ascii=False)


class ForwardSearchOrchestrator:
    def __init__(
        self,
        config: dict[str, Any],
        pixiv: PixivForwardSearchService | None,
        soutu: SoutuSearchService | None,
        serpapi: SerpApiForwardService | None,
    ) -> None:
        self.config = config
        self.pixiv = pixiv
        self.soutu = soutu
        self.serpapi = serpapi

    def _source_config(self, source: str) -> dict[str, Any]:
        value = self.config.get(source, {})
        return value if isinstance(value, dict) else {}

    def _available(self, source: str) -> bool:
        if source == "pixiv":
            return self.pixiv is not None and self._source_config(source).get(
                "enabled", True
            )
        if source == "soutu":
            return self.soutu is not None and self._source_config(source).get(
                "enabled", True
            )
        if source == "serpapi":
            return (
                self.serpapi is not None
                and self.serpapi.available()
                and self._source_config(source).get("enabled", True)
            )
        return False

    @staticmethod
    def _looks_like_pixiv(query: str) -> bool:
        lowered = query.lower()
        if any(hint in lowered for hint in _PIXIV_HINTS):
            return True
        return bool(re.search(r"[\u3040-\u30ff]", query))

    def _configured_fallback_order(self) -> list[str]:
        raw = self.config.get("fallback_order", ["pixiv", "soutu", "serpapi"])
        if isinstance(raw, str):
            raw = re.split(r"[,，;；\s]+", raw)
        if not isinstance(raw, list):
            raw = []
        normalized = [str(item).strip().lower() for item in raw]
        return list(dict.fromkeys(item for item in normalized if item in SOURCE_NAMES))

    def choose_sources(self, query: str, requested: str) -> list[str]:
        requested = str(requested or "auto").strip().lower()
        fallback = self._configured_fallback_order()
        if requested in SOURCE_NAMES:
            order = [requested]
            if self.config.get("fallback_enabled", True):
                order.extend(source for source in fallback if source != requested)
        else:
            if self.config.get("auto_source_enabled", True):
                preferred = "pixiv" if self._looks_like_pixiv(query) else "soutu"
            else:
                preferred = str(self.config.get("default_source") or "soutu").lower()
                if preferred not in SOURCE_NAMES:
                    preferred = "soutu"
            order = [preferred]
            order.extend(source for source in fallback if source != preferred)
        return [source for source in dict.fromkeys(order) if self._available(source)]

    async def close(self) -> None:
        if self.pixiv:
            await self.pixiv.close()
        if self.soutu:
            await self.soutu.terminate()
        if self.serpapi:
            await self.serpapi.close()

    async def search(
        self,
        event: AstrMessageEvent,
        query: str,
        description: str = "",
        source: str = "auto",
        count: int = 1,
        *,
        for_command: bool = False,
    ) -> ForwardOutcome:
        outcome = ForwardOutcome()
        query = str(query or "").strip()
        if not query:
            outcome.errors["request"] = "搜索关键词不能为空。"
            return outcome
        try:
            count = max(1, min(int(count), 5))
        except (TypeError, ValueError):
            count = 1

        review_cfg = self.config.get("llm_review", {})
        if not isinstance(review_cfg, dict):
            review_cfg = {}
        review_enabled = bool(review_cfg.get("enabled", True))
        if for_command and not review_cfg.get("commands_enabled", True):
            review_enabled = False
        review_fail_open = bool(review_cfg.get("fail_open", True))
        send_images = bool(self.config.get("tool_send_images", True)) or for_command

        sources = self.choose_sources(query, source)
        if not sources:
            outcome.errors["configuration"] = "没有已启用且配置完整的找图来源。"
            return outcome

        for current in sources:
            outcome.attempted_sources.append(current)
            try:
                if current == "pixiv" and self.pixiv:
                    result = await self.pixiv.search(
                        event,
                        query,
                        description or query,
                        count=count,
                        review_enabled=review_enabled,
                        send_images=send_images,
                    )
                    if result.success:
                        outcome.success = True
                        outcome.source = current
                        outcome.message_sent = result.sent_count > 0
                        outcome.review_fallback = result.review_fallback
                        outcome.pixiv_ids = result.ids
                        return outcome
                    outcome.errors[current] = result.error or "Pixiv 找图失败。"

                elif current == "soutu" and self.soutu:
                    source_cfg = self._source_config(current)
                    use_vlm = review_enabled and bool(
                        source_cfg.get("vlm_selection_enabled", True)
                    )
                    image_bytes, error, review_fallback = await self.soutu.search(
                        event,
                        query,
                        description or query,
                        use_vlm_selection=use_vlm,
                    )
                    if image_bytes:
                        if review_enabled and review_fallback and not review_fail_open:
                            outcome.errors[current] = (
                                "视觉审核不可用或未选出匹配图片，按配置不放行首图。"
                            )
                        else:
                            if send_images:
                                await event.send(
                                    event.chain_result(
                                        [Comp.Image.fromBytes(image_bytes)]
                                    )
                                )
                            outcome.success = True
                            outcome.source = current
                            outcome.message_sent = send_images
                            outcome.review_fallback = review_fallback
                            return outcome
                    if not image_bytes:
                        outcome.errors[current] = error or "搜图神器来源没有结果。"

                elif current == "serpapi" and self.serpapi:
                    source_cfg = self._source_config(current)
                    use_vlm = review_enabled and bool(
                        source_cfg.get("vlm_selection_enabled", True)
                    )
                    result = await self.serpapi.search(
                        event,
                        query,
                        description or query,
                        review_enabled=use_vlm,
                    )
                    if result.image_bytes:
                        if use_vlm and result.review_fallback and not review_fail_open:
                            outcome.errors[current] = (
                                "视觉审核不可用或未选出匹配图片，按配置不放行首图。"
                            )
                        else:
                            if send_images:
                                await event.send(
                                    event.chain_result(
                                        [Comp.Image.fromBytes(result.image_bytes)]
                                    )
                                )
                            outcome.success = True
                            outcome.source = current
                            outcome.message_sent = send_images
                            outcome.review_fallback = result.review_fallback
                            return outcome
                    if not result.image_bytes:
                        outcome.errors[current] = result.error or "SerpApi 没有结果。"
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[AliceImageForward] 来源 %s 执行异常: %s",
                    current,
                    exc,
                    exc_info=True,
                )
                outcome.errors[current] = str(exc)

            if not self.config.get("fallback_enabled", True):
                break

        return outcome
