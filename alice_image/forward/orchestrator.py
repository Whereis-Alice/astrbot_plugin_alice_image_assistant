"""统一文字找图、自动选源与失败回退。

本模块负责：按查询语义选源 → 可选的查询改写（多语言检索式）→ 逐源或跨源并发检索
→ 依据视觉审核结果决定放行/换源 → 发送并汇总决策依据。
所有匹配判定都以用户原始描述为准，改写结果只用于"检索"，不用于"判定"。
"""

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
from .query_rewrite import (
    DEFAULT_CACHE_SIZE,
    QueryRewriter,
    RewrittenQuery,
    fallback_query,
)
from .review import ReviewStatus, review_status_value
from .serpapi.service import SerpApiForwardService
from .source_selection import looks_like_pixiv as _looks_like_pixiv_impl
from .soutu.service import SoutuSearchService

SOURCE_NAMES = ("pixiv", "soutu", "serpapi")
_REAL_IMAGE_AUTO_ORDER = ("soutu", "serpapi", "pixiv")
_LEGACY_FALLBACK_ORDER = ("pixiv", "soutu", "serpapi")
# soutu / serpapi 都直接产出图片字节，处理流程同构，可共用一套「执行→判定→发送」逻辑。
_BYTE_SOURCES = ("soutu", "serpapi")

MIN_IMAGE_COUNT = 1
MAX_IMAGE_COUNT = 5

_SOURCE_LABELS = {
    "pixiv": "Pixiv",
    "soutu": "搜图神器来源",
    "serpapi": "SerpApi",
}
_NO_MATCH_HINTS = {
    "soutu": "视觉审核明确判定当前来源候选均与描述不匹配。",
    "serpapi": "视觉审核明确判定 SerpApi 候选均与描述不匹配。",
}
_NO_RESULT_HINTS = {
    "soutu": "搜图神器来源没有结果。",
    "serpapi": "SerpApi 没有结果。",
}

# 跨源择优的评分梯度：只有"被终选复核明确判定 match"的候选才拿到真实 confidence，
# 未审核候选给中等分，靠 fail-open / 非严格模式放行的候选给最低分，
# 这样 best_of 永远优先发送"验证过"的图，而不是碰巧先返回的图。
_SCORE_MATCHED_DEFAULT = 0.5
_SCORE_NOT_REVIEWED = 0.3
_SCORE_FALLBACK = 0.1
_SCORE_UNUSABLE = -1.0


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
    review_confidence: float | None = None
    review_reason: str = ""
    source_scores: dict[str, float] = field(default_factory=dict)
    query_rewrite: dict[str, Any] = field(default_factory=dict)
    pixiv_ids: list[int] = field(default_factory=list)
    pixiv_found_count: int = 0
    pixiv_artist_user_id: int | None = None
    pixiv_artist_name: str = ""
    pixiv_artist_account: str = ""

    def to_json(self) -> str:
        payload = asdict(self)
        if self.success and self.message_sent:
            instruction = "图片已发送，请简短说明使用的来源；不要虚构图片内容。"
        elif self.success and self.delivery_uncertain:
            instruction = (
                "图片来源已找到并已尝试发送，但平台发送确认超时或失败；"
                "请简短告知用户发送状态不确定，不要切换其它图源，也不要虚构图片内容。"
            )
        elif self.success and self.send_attempted:
            instruction = (
                "图片来源已找到并已尝试发送；请简短说明使用的来源，不要虚构图片内容。"
            )
        elif self.success:
            instruction = (
                "图片来源已找到，但当前配置不自动发送图片；"
                "请简短说明使用的来源和可用结果，不要虚构图片内容。"
            )
        else:
            instruction = "请根据 errors 向用户说明失败原因，并建议调整关键词或配置。"
        payload["instruction"] = instruction
        return json.dumps(payload, ensure_ascii=False)


@dataclass(slots=True)
class _ByteSourceAttempt:
    """一次 soutu / serpapi 检索的执行结果与审核元数据。"""

    source: str
    used_vlm: bool = False
    result: Any | None = None
    error: str = ""
    review_status: str = ""
    confidence: float | None = None
    reason: str = ""
    score: float = _SCORE_UNUSABLE


def _coerce_score(value: Any) -> float | None:
    """把审核置信度收敛到 0..1 的浮点数；无法解析时返回 None（视为"未提供"）。"""
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, parsed))


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
        self._rewriter: QueryRewriter | None = None
        self._rewriter_signature: tuple[Any, ...] | None = None

    def _source_config(self, source: str) -> dict[str, Any]:
        value = self.config.get(source, {})
        return value if isinstance(value, dict) else {}

    def _review_config(self) -> dict[str, Any]:
        value = self.config.get("llm_review", {})
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
        # 判定逻辑已抽到 source_selection：反向关键词（照片/写真/真人/cos…）优先于
        # 正向关键词与假名，避免"真人 cos 动漫角色"被误判成插画需求而走 Pixiv。
        return _looks_like_pixiv_impl(query)

    def _configured_fallback_order(self) -> list[str]:
        raw = self.config.get("fallback_order", ["soutu", "serpapi", "pixiv"])
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
                logger.info("[AliceImageForward] 来源 %s 后台发送任务已完成。", source)
            except Exception as exc:
                logger.warning(
                    "[AliceImageForward] 来源 %s 后台发送任务最终失败: %s",
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
                f"发送等待超过 {timeout_seconds:g} 秒，已转入后台继续等待平台确认",
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
        except Exception as exc:
            logger.warning(
                "[AliceImageForward] 来源 %s 已找到图片但发送失败: %s",
                source,
                exc,
            )
            return False, str(exc)

    # ---------------- 数量与配置 ----------------

    def _normalize_count(self, raw: Any, outcome: ForwardOutcome) -> int:
        """把请求数量收敛到受支持区间；越界时显式告知调用方而不是静默裁剪。"""
        try:
            parsed = int(raw)
        except (TypeError, ValueError):
            message = f"请求数量 {raw!r} 无法解析为整数，已按 {MIN_IMAGE_COUNT} 张处理。"
            logger.warning("[AliceImageForward] %s", message)
            outcome.warnings["count"] = message
            return MIN_IMAGE_COUNT
        clamped = max(MIN_IMAGE_COUNT, min(parsed, MAX_IMAGE_COUNT))
        if clamped != parsed:
            message = (
                f"请求数量 {parsed} 超出支持范围 "
                f"{MIN_IMAGE_COUNT}~{MAX_IMAGE_COUNT}，已按 {clamped} 张处理。"
            )
            logger.warning("[AliceImageForward] %s", message)
            outcome.warnings["count"] = message
        return clamped

    def _configure_source_review(self, review_cfg: dict[str, Any]) -> None:
        """把审核相关配置（终选复核开关、置信度阈值、并发上限）下发给各字节来源。"""
        for service in (self.soutu, self.serpapi):
            configure = getattr(service, "configure_review", None)
            if configure is None:
                continue
            try:
                configure(review_cfg)
            except Exception as exc:
                logger.warning(
                    "[AliceImageForward] 下发审核配置失败（%s）：%s",
                    type(service).__name__,
                    exc,
                )

    def _best_of_enabled(self) -> bool:
        return bool(self.config.get("best_of_enabled", False))

    # ---------------- 查询改写 ----------------

    def _query_rewriter(self) -> QueryRewriter:
        """复用同一个改写器实例，让 LRU 缓存能真正命中，避免重复 LLM 调用。"""
        cfg = self.config.get("query_rewrite", {})
        if not isinstance(cfg, dict):
            cfg = {}
        signature = (
            bool(cfg.get("enabled", True)),
            str(cfg.get("provider_id") or "").strip(),
            cfg.get("cache_size", DEFAULT_CACHE_SIZE),
        )
        if self._rewriter is None or self._rewriter_signature != signature:
            self._rewriter = QueryRewriter(cfg)
            self._rewriter_signature = signature
        return self._rewriter

    async def _resolve_rewrite_provider(
        self,
        event: AstrMessageEvent,
        provider_id: str,
        agent_run_context: Any | None,
    ) -> Any | None:
        """复用各来源已有的审核 provider 解析逻辑，不新增 provider 配置通道。"""
        for service in (self.soutu, self.serpapi, self.pixiv):
            resolver = getattr(service, "review_resolver", None)
            if resolver is None or not hasattr(resolver, "resolve"):
                continue
            try:
                provider = await resolver.resolve(
                    event,
                    provider_id,
                    agent_run_context=agent_run_context,
                    log_prefix="AliceImageQueryRewrite",
                )
            except Exception as exc:
                logger.warning(
                    "[AliceImageQueryRewrite] 解析改写模型失败，继续尝试其它来源：%s",
                    exc,
                )
                continue
            if provider is not None:
                return provider
        logger.debug("[AliceImageQueryRewrite] 未找到可用改写模型，按原查询检索。")
        return None

    async def _rewrite_query(
        self,
        event: AstrMessageEvent,
        query: str,
        description: str,
        agent_run_context: Any | None,
    ) -> RewrittenQuery:
        """把口语化描述改写成面向图搜的检索式；任何失败都原样回退。"""
        rewriter = self._query_rewriter()
        if not rewriter.enabled or not query:
            return fallback_query(query)
        provider = await self._resolve_rewrite_provider(
            event,
            rewriter.provider_id,
            agent_run_context,
        )
        return await rewriter.rewrite(provider, query, description=description)

    # ---------------- 字节来源（soutu / serpapi） ----------------

    async def _run_byte_source(
        self,
        event: AstrMessageEvent,
        source: str,
        search_query: str,
        description: str,
        *,
        review_enabled: bool,
        strict_match_enabled: bool,
        agent_run_context: Any | None,
    ) -> _ByteSourceAttempt:
        source_cfg = self._source_config(source)
        use_vlm = review_enabled and bool(
            source_cfg.get("vlm_selection_enabled", True)
        )
        attempt = _ByteSourceAttempt(source=source, used_vlm=use_vlm)
        service = self.soutu if source == "soutu" else self.serpapi
        if service is None:
            attempt.error = f"{_SOURCE_LABELS.get(source, source)}未启用。"
            return attempt
        try:
            if source == "soutu":
                result = await service.search(
                    event,
                    search_query,
                    description,
                    use_vlm_selection=use_vlm,
                    strict_match_enabled=strict_match_enabled,
                    agent_run_context=agent_run_context,
                )
            else:
                result = await service.search(
                    event,
                    search_query,
                    description,
                    review_enabled=use_vlm,
                    strict_match_enabled=strict_match_enabled,
                    agent_run_context=agent_run_context,
                )
        except Exception as exc:
            logger.error(
                "[AliceImageForward] 来源 %s 执行异常: %s",
                source,
                exc,
                exc_info=True,
            )
            attempt.error = str(exc)
            return attempt

        attempt.result = result
        attempt.review_status = review_status_value(
            getattr(result, "review_status", ReviewStatus.NOT_RUN)
        )
        attempt.confidence = _coerce_score(getattr(result, "review_confidence", None))
        attempt.reason = str(getattr(result, "review_reason", "") or "")
        attempt.score = self._score_attempt(attempt)
        return attempt

    @staticmethod
    def _score_attempt(attempt: _ByteSourceAttempt) -> float:
        """给候选打分，供跨源择优排序；未被明确验证的候选一律低于已验证候选。"""
        result = attempt.result
        if result is None or not getattr(result, "image_bytes", None):
            return _SCORE_UNUSABLE
        if attempt.review_status == ReviewStatus.MATCHED.value:
            if attempt.confidence is not None:
                return attempt.confidence
            return _SCORE_MATCHED_DEFAULT
        if attempt.review_status == ReviewStatus.NOT_RUN.value:
            return _SCORE_NOT_REVIEWED
        return _SCORE_FALLBACK

    def _decide_byte_source(
        self,
        attempt: _ByteSourceAttempt,
        *,
        strict_match_enabled: bool,
        review_fail_open: bool,
    ) -> str:
        """返回空串表示该候选可以发送，否则返回应写入 errors 的失败原因。"""
        source = attempt.source
        label = _SOURCE_LABELS.get(source, source)
        result = attempt.result
        if result is None:
            return attempt.error or f"{label}执行失败。"
        # -1（全不匹配）由 strict_match_enabled 决定是否换源。
        if (
            attempt.used_vlm
            and strict_match_enabled
            and attempt.review_status == ReviewStatus.NO_MATCH.value
        ):
            message = str(getattr(result, "error", "") or "")
            if "不匹配" not in message:
                hint = _NO_MATCH_HINTS.get(source, "视觉审核判定候选与描述不匹配。")
                message = f"{message}；{hint}".lstrip("；")
            return message
        if not getattr(result, "image_bytes", None):
            return (
                str(getattr(result, "error", "") or "")
                or attempt.error
                or _NO_RESULT_HINTS.get(source, f"{label}没有结果。")
            )
        # -2（审核链路本身出错）由 review_fail_open 决定是否放行首图。
        if (
            attempt.used_vlm
            and attempt.review_status == ReviewStatus.ERROR.value
            and not review_fail_open
        ):
            return "视觉审核模型不可用或调用失败，按配置不放行首图。"
        return ""

    async def _finalize_byte_source(
        self,
        event: AstrMessageEvent,
        attempt: _ByteSourceAttempt,
        outcome: ForwardOutcome,
        *,
        send_images: bool,
        send_wait_timeout_seconds: float,
    ) -> ForwardOutcome:
        result = attempt.result
        image_bytes = getattr(result, "image_bytes", None) or b""
        if send_images:
            sent, send_error = await self._send_image_bytes(
                event,
                attempt.source,
                image_bytes,
                send_wait_timeout_seconds,
            )
        else:
            sent, send_error = False, ""
        outcome.success = True
        outcome.source = attempt.source
        outcome.message_sent = sent
        outcome.send_attempted = send_images
        outcome.delivery_uncertain = send_images and not sent
        if send_error:
            outcome.warnings[attempt.source] = (
                "来源已找到图片，但平台发送确认超时或失败；不会切换其它图源。"
                f"最后一次发送错误：{send_error}"
            )
        outcome.review_fallback = bool(getattr(result, "review_fallback", False))
        outcome.review_confidence = attempt.confidence
        outcome.review_reason = attempt.reason
        return outcome

    # ---------------- Pixiv ----------------

    async def _try_pixiv(
        self,
        event: AstrMessageEvent,
        outcome: ForwardOutcome,
        search_query: str,
        description: str,
        *,
        count: int,
        review_enabled: bool,
        send_images: bool,
        send_wait_timeout_seconds: float,
        artist_name: str,
        pixiv_user_id: str,
        agent_run_context: Any | None,
    ) -> bool:
        if self.pixiv is None:
            outcome.errors["pixiv"] = "Pixiv 来源未启用。"
            return False
        result = await self.pixiv.search(
            event,
            search_query,
            description,
            count=count,
            review_enabled=review_enabled,
            send_images=send_images,
            send_wait_timeout_seconds=send_wait_timeout_seconds,
            artist_name=artist_name,
            pixiv_user_id=pixiv_user_id,
            agent_run_context=agent_run_context,
        )
        if not result.success:
            outcome.errors["pixiv"] = result.error or "Pixiv 找图失败。"
            return False
        outcome.success = True
        outcome.source = "pixiv"
        outcome.message_sent = result.sent_count > 0
        outcome.send_attempted = bool(
            getattr(result, "send_attempted", result.sent_count > 0)
        )
        outcome.delivery_uncertain = bool(
            getattr(result, "delivery_uncertain", False)
        )
        outcome.review_fallback = result.review_fallback
        outcome.review_confidence = _coerce_score(
            getattr(result, "review_confidence", None)
        )
        outcome.review_reason = str(getattr(result, "review_reason", "") or "")
        outcome.pixiv_ids = result.ids
        outcome.pixiv_found_count = int(
            getattr(result, "found_count", len(result.ids))
        )
        outcome.pixiv_artist_user_id = getattr(result, "artist_user_id", None)
        outcome.pixiv_artist_name = str(getattr(result, "artist_name", "") or "")
        outcome.pixiv_artist_account = str(getattr(result, "artist_account", "") or "")
        if result.error:
            outcome.warnings["pixiv"] = result.error
        return True

    # ---------------- 主流程 ----------------

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
        agent_run_context: Any | None = None,
    ) -> ForwardOutcome:
        outcome = ForwardOutcome()
        query = str(query or "").strip()
        artist_name = str(artist_name or "").strip()
        pixiv_user_id = str(pixiv_user_id or "").strip()
        artist_scoped = bool(artist_name or pixiv_user_id)
        if not query and not artist_scoped:
            outcome.errors["request"] = "搜索关键词不能为空。"
            return outcome
        count = self._normalize_count(count, outcome)

        review_cfg = self._review_config()
        review_enabled = bool(review_cfg.get("enabled", True))
        if for_command and not review_cfg.get("commands_enabled", True):
            review_enabled = False
        review_fail_open = bool(review_cfg.get("fail_open", True))
        strict_match_enabled = bool(review_cfg.get("strict_match_enabled", True))
        self._configure_source_review(review_cfg)
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
                "指定画师找图需要启用并配置 Pixiv 来源。"
                if artist_scoped
                else "没有已启用且配置完整的找图来源。"
            )
            return outcome

        # 匹配判定永远用用户原始描述，改写结果只用于检索，避免把改写偏差当成真值。
        eval_description = description or query
        rewritten = fallback_query(query)
        if query and not artist_scoped:
            rewritten = await self._rewrite_query(
                event,
                query,
                eval_description,
                agent_run_context,
            )
        outcome.query_rewrite = dict(rewritten.to_json_dict())

        fallback_enabled = bool(self.config.get("fallback_enabled", True))
        byte_sources = [item for item in sources if item in _BYTE_SOURCES]
        if self._best_of_enabled() and fallback_enabled and len(byte_sources) >= 2:
            finished = await self._search_best_of(
                event,
                outcome,
                byte_sources,
                rewritten,
                eval_description,
                review_enabled=review_enabled,
                strict_match_enabled=strict_match_enabled,
                review_fail_open=review_fail_open,
                send_images=send_images,
                send_wait_timeout_seconds=send_wait_timeout_seconds,
                agent_run_context=agent_run_context,
            )
            if finished:
                return outcome
            sources = [item for item in sources if item not in _BYTE_SOURCES]

        for current in sources:
            outcome.attempted_sources.append(current)
            try:
                if current == "pixiv":
                    if await self._try_pixiv(
                        event,
                        outcome,
                        rewritten.for_source("pixiv") if not artist_scoped else query,
                        eval_description,
                        count=count,
                        review_enabled=review_enabled,
                        send_images=send_images,
                        send_wait_timeout_seconds=send_wait_timeout_seconds,
                        artist_name=artist_name,
                        pixiv_user_id=pixiv_user_id,
                        agent_run_context=agent_run_context,
                    ):
                        return outcome
                else:
                    attempt = await self._run_byte_source(
                        event,
                        current,
                        rewritten.for_source(current),
                        eval_description,
                        review_enabled=review_enabled,
                        strict_match_enabled=strict_match_enabled,
                        agent_run_context=agent_run_context,
                    )
                    outcome.source_scores[current] = attempt.score
                    failure = self._decide_byte_source(
                        attempt,
                        strict_match_enabled=strict_match_enabled,
                        review_fail_open=review_fail_open,
                    )
                    if not failure:
                        return await self._finalize_byte_source(
                            event,
                            attempt,
                            outcome,
                            send_images=send_images,
                            send_wait_timeout_seconds=send_wait_timeout_seconds,
                        )
                    outcome.errors[current] = failure
            except Exception as exc:
                logger.error(
                    "[AliceImageForward] 来源 %s 执行异常: %s",
                    current,
                    exc,
                    exc_info=True,
                )
                outcome.errors[current] = str(exc)

            if not fallback_enabled:
                break

        return outcome

    async def _search_best_of(
        self,
        event: AstrMessageEvent,
        outcome: ForwardOutcome,
        byte_sources: list[str],
        rewritten: RewrittenQuery,
        eval_description: str,
        *,
        review_enabled: bool,
        strict_match_enabled: bool,
        review_fail_open: bool,
        send_images: bool,
        send_wait_timeout_seconds: float,
        agent_run_context: Any | None,
    ) -> bool:
        """并发跑多个字节来源并按置信度择优；返回 True 表示已产出最终结果。"""
        # 只比"通过判定"的候选，并按终选复核置信度排序，
        # 这样即使某个源先返回也不会抢占更贴合描述的候选。
        outcome.attempted_sources.extend(byte_sources)
        attempts = await asyncio.gather(
            *(
                self._run_byte_source(
                    event,
                    item,
                    rewritten.for_source(item),
                    eval_description,
                    review_enabled=review_enabled,
                    strict_match_enabled=strict_match_enabled,
                    agent_run_context=agent_run_context,
                )
                for item in byte_sources
            ),
            return_exceptions=True,
        )
        usable: list[_ByteSourceAttempt] = []
        for item, attempt in zip(byte_sources, attempts, strict=False):
            if isinstance(attempt, BaseException):
                logger.error(
                    "[AliceImageForward] 跨源择优时来源 %s 异常: %s",
                    item,
                    attempt,
                )
                outcome.errors[item] = str(attempt)
                outcome.source_scores[item] = _SCORE_UNUSABLE
                continue
            outcome.source_scores[item] = attempt.score
            failure = self._decide_byte_source(
                attempt,
                strict_match_enabled=strict_match_enabled,
                review_fail_open=review_fail_open,
            )
            if failure:
                outcome.errors[item] = failure
                continue
            usable.append(attempt)

        if not usable:
            return False
        best = max(usable, key=lambda item: item.score)
        logger.info(
            "[AliceImageForward] 跨源择优选中 %s（评分 %.3f，各源评分 %s）。",
            best.source,
            best.score,
            outcome.source_scores,
        )
        outcome.errors.pop(best.source, None)
        await self._finalize_byte_source(
            event,
            best,
            outcome,
            send_images=send_images,
            send_wait_timeout_seconds=send_wait_timeout_seconds,
        )
        return True
