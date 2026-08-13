"""?????????????????"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .pixiv_search import PixivForwardSearchService
from .review import ReviewStatus, review_status_value
from .serpapi.service import SerpApiForwardService
from .soutu.service import SoutuSearchService

SOURCE_NAMES = ("pixiv", "soutu", "serpapi")
_REAL_IMAGE_AUTO_ORDER = ("soutu", "serpapi", "pixiv")
_LEGACY_FALLBACK_ORDER = ("pixiv", "soutu", "serpapi")
_PIXIV_HINTS = (
    "pixiv",
    "p?",
    "???",
    "??",
    "??",
    "??",
    "??",
    "??",
    "??",
    "??",
    "vtuber",
    "vocaloid",
)


@dataclass(slots=True)
class ForwardOutcome:
    success: bool = False
    source: str = ""
    attempted_sources: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    warnings: dict[str, str] = field(default_factory=dict)
    message_sent: bool = False
    send_attempted: bool = False
    delivery_uncertain: bool = False
    review_fallback: bool = False
    pixiv_ids: list[int] = field(default_factory=list)
    pixiv_found_count: int = 0
    pixiv_artist_user_id: int | None = None
    pixiv_artist_name: str = ""
    pixiv_artist_account: str = ""

    def to_json(self) -> str:
        payload = asdict(self)
        if self.success and self.message_sent:
            instruction = "??????????????????????????"
        elif self.success and self.delivery_uncertain:
            instruction = (
                "???????????????????????????"
                "??????????????????????????????????"
            )
        elif self.success and self.send_attempted:
            instruction = (
                "??????????????????????????????????"
            )
        elif self.success:
            instruction = (
                "?????????????????????"
                "?????????????????????????"
            )
        else:
            instruction = "??? errors ??????????????????????"
        payload["instruction"] = instruction
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
        self._background_send_tasks: set[asyncio.Task[None]] = set()

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
        raw = self.config.get("fallback_order", ["soutu", "serpapi", "pixiv"])
        if isinstance(raw, str):
            raw = re.split(r"[,?;?\s]+", raw)
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
            remaining = [source for source in fallback if source != preferred]
            if preferred == "soutu" and tuple(fallback) == _LEGACY_FALLBACK_ORDER:
                remaining.sort(
                    key=lambda source: _REAL_IMAGE_AUTO_ORDER.index(source)
                )
            order.extend(remaining)
        return [source for source in dict.fromkeys(order) if self._available(source)]

    async def close(self) -> None:
        for task in list(self._background_send_tasks):
            task.cancel()
        if self._background_send_tasks:
            await asyncio.gather(
                *self._background_send_tasks,
                return_exceptions=True,
            )
            self._background_send_tasks.clear()
        if self.pixiv:
            await self.pixiv.close()
        if self.soutu:
            await self.soutu.terminate()
        if self.serpapi:
            await self.serpapi.close()

    def _send_wait_timeout_seconds(self) -> float:
        raw = self.config.get("tool_send_wait_timeout_seconds", 45)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = 45
        return max(0, min(value, 600))

    def _track_background_send(
        self,
        source: str,
        task: asyncio.Task[None],
    ) -> None:
        self._background_send_tasks.add(task)

        def _done(done_task: asyncio.Task[None]) -> None:
            self._background_send_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                done_task.result()
                logger.info("[AliceImageForward] ?? %s ??????????", source)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AliceImageForward] ?? %s ??????????: %s",
                    source,
                    exc,
                )

        task.add_done_callback(_done)

    async def _send_with_wait_limit(
        self,
        event: AstrMessageEvent,
        result: Any,
        source: str,
        timeout_seconds: float,
    ) -> tuple[bool, str]:
        if timeout_seconds <= 0:
            await event.send(result)
            return True, ""

        task = asyncio.create_task(event.send(result))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
            return True, ""
        except TimeoutError:
            self._track_background_send(source, task)
            return (
                False,
                f"?????? {timeout_seconds:g} ???????????????",
            )

    async def _send_image_bytes(
        self,
        event: AstrMessageEvent,
        source: str,
        image_bytes: bytes,
        timeout_seconds: float,
    ) -> tuple[bool, str]:
        try:
            return await self._send_with_wait_limit(
                event,
                event.chain_result([Comp.Image.fromBytes(image_bytes)]),
                source,
                timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AliceImageForward] ?? %s ??????????: %s",
                source,
                exc,
            )
            return False, str(exc)

    async def search(
        self,
        event: AstrMessageEvent,
        query: str,
        description: str = "",
        source: str = "auto",
        count: int = 1,
        *,
        for_command: bool = False,
        artist_name: str = "",
        pixiv_user_id: str | int = "",
    ) -> ForwardOutcome:
        outcome = ForwardOutcome()
        query = str(query or "").strip()
        artist_name = str(artist_name or "").strip()
        pixiv_user_id = str(pixiv_user_id or "").strip()
        artist_scoped = bool(artist_name or pixiv_user_id)
        if not query and not artist_scoped:
            outcome.errors["request"] = "??????????"
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
        strict_match_enabled = bool(
            review_cfg.get("strict_match_enabled", True)
        )
        send_images = bool(self.config.get("tool_send_images", True)) or for_command
        send_wait_timeout_seconds = (
            0 if for_command else self._send_wait_timeout_seconds()
        )

        if artist_scoped:
            sources = ["pixiv"] if self._available("pixiv") else []
        else:
            sources = self.choose_sources(query, source)
        if not sources:
            outcome.errors["configuration"] = (
                "????????????? Pixiv ???"
                if artist_scoped
                else "????????????????"
            )
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
                        send_wait_timeout_seconds=send_wait_timeout_seconds,
                        artist_name=artist_name,
                        pixiv_user_id=pixiv_user_id,
                    )
                    if result.success:
                        outcome.success = True
                        outcome.source = current
                        outcome.message_sent = result.sent_count > 0
                        outcome.send_attempted = bool(
                            getattr(result, "send_attempted", result.sent_count > 0)
                        )
                        outcome.delivery_uncertain = bool(
                            getattr(result, "delivery_uncertain", False)
                        )
                        outcome.review_fallback = result.review_fallback
                        outcome.pixiv_ids = result.ids
                        outcome.pixiv_found_count = int(
                            getattr(result, "found_count", len(result.ids))
                        )
                        outcome.pixiv_artist_user_id = getattr(
                            result, "artist_user_id", None
                        )
                        outcome.pixiv_artist_name = str(
                            getattr(result, "artist_name", "") or ""
                        )
                        outcome.pixiv_artist_account = str(
                            getattr(result, "artist_account", "") or ""
                        )
                        if result.error:
                            outcome.warnings[current] = result.error
                        return outcome
                    outcome.errors[current] = result.error or "Pixiv ?????"

                elif current == "soutu" and self.soutu:
                    source_cfg = self._source_config(current)
                    use_vlm = review_enabled and bool(
                        source_cfg.get("vlm_selection_enabled", True)
                    )
                    result = await self.soutu.search(
                        event,
                        query,
                        description or query,
                        use_vlm_selection=use_vlm,
                        strict_match_enabled=strict_match_enabled,
                    )
                    review_status = review_status_value(
                        getattr(result, "review_status", ReviewStatus.NOT_RUN)
                    )
                    if (
                        use_vlm
                        and strict_match_enabled
                        and review_status == ReviewStatus.NO_MATCH.value
                    ):
                        outcome.errors[current] = (
                            result.error or ""
                        )
                        if "???" not in outcome.errors[current]:
                            outcome.errors[current] = (
                                f"{outcome.errors[current]}?"
                                "??????????????????????"
                            ).lstrip("?")
                    elif result.image_bytes:
                        if (
                            use_vlm
                            and review_status == ReviewStatus.ERROR.value
                            and not review_fail_open
                        ):
                            outcome.errors[current] = (
                                "????????????????????????"
                            )
                        else:
                            if send_images:
                                sent, send_error = await self._send_image_bytes(
                                    event,
                                    current,
                                    result.image_bytes,
                                    send_wait_timeout_seconds,
                                )
                            else:
                                sent, send_error = False, ""
                            outcome.success = True
                            outcome.source = current
                            outcome.message_sent = sent
                            outcome.send_attempted = send_images
                            outcome.delivery_uncertain = send_images and not sent
                            if send_error:
                                outcome.warnings[current] = (
                                    "??????????????????????????????"
                                    f"?????????{send_error}"
                                )
                            outcome.review_fallback = result.review_fallback
                            return outcome
                    else:
                        outcome.errors[current] = (
                            result.error or "???????????"
                        )

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
                        strict_match_enabled=strict_match_enabled,
                    )
                    review_status = review_status_value(
                        getattr(result, "review_status", ReviewStatus.NOT_RUN)
                    )
                    if (
                        use_vlm
                        and strict_match_enabled
                        and review_status == ReviewStatus.NO_MATCH.value
                    ):
                        outcome.errors[current] = (
                            result.error or ""
                        )
                        if "???" not in outcome.errors[current]:
                            outcome.errors[current] = (
                                f"{outcome.errors[current]}?"
                                "???????? SerpApi ??????????"
                            ).lstrip("?")
                    elif result.image_bytes:
                        if (
                            use_vlm
                            and review_status == ReviewStatus.ERROR.value
                            and not review_fail_open
                        ):
                            outcome.errors[current] = (
                                "????????????????????????"
                            )
                        else:
                            if send_images:
                                sent, send_error = await self._send_image_bytes(
                                    event,
                                    current,
                                    result.image_bytes,
                                    send_wait_timeout_seconds,
                                )
                            else:
                                sent, send_error = False, ""
                            outcome.success = True
                            outcome.source = current
                            outcome.message_sent = sent
                            outcome.send_attempted = send_images
                            outcome.delivery_uncertain = send_images and not sent
                            if send_error:
                                outcome.warnings[current] = (
                                    "??????????????????????????????"
                                    f"?????????{send_error}"
                                )
                            outcome.review_fallback = result.review_fallback
                            return outcome
                    else:
                        outcome.errors[current] = result.error or "SerpApi ?????"
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[AliceImageForward] ?? %s ????: %s",
                    current,
                    exc,
                    exc_info=True,
                )
                outcome.errors[current] = str(exc)

            if not self.config.get("fallback_enabled", True):
                break

        return outcome
