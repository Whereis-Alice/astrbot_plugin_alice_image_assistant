"""面向 LLM 的精确 Pixiv 找图与可选视觉审核。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context

from ..pixiv.controller import AlicePixivController
from ..pixiv.utils.pixiv_utils import (
    download_image,
    send_forward_message,
    send_pixiv_image,
)
from ..pixiv.utils.tag import (
    FilterConfig,
    build_detail_message,
    filter_illusts_with_reason,
    process_and_send_illusts_sorted,
    validate_and_process_tags,
)
from .serpapi.vlm import select_from_collage
from .soutu.composer import ComposerManager


@dataclass(slots=True)
class PixivForwardResult:
    success: bool = False
    error: str = ""
    ids: list[int] = field(default_factory=list)
    sent_count: int = 0
    review_fallback: bool = False


class PixivForwardSearchService:
    def __init__(
        self,
        context: Context,
        controller: AlicePixivController,
        review_config: dict[str, Any] | None = None,
    ) -> None:
        self.context = context
        self.controller = controller
        self.review_config = review_config or {}
        self._collage = ComposerManager()
        self._review_lock = asyncio.Semaphore(
            self._bounded_int("max_concurrency", 2, 1, 8)
        )

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.review_config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    async def close(self) -> None:
        await self._collage.close_all()

    async def _provider(self, event: AstrMessageEvent):
        provider_id = str(self.review_config.get("provider_id") or "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider
            logger.warning(
                "[AliceImagePixiv] 找不到审核模型 %s，回退当前会话模型",
                provider_id,
            )
        current_id = await self.context.get_current_chat_provider_id(
            event.unified_msg_origin
        )
        return self.context.get_provider_by_id(current_id) if current_id else None

    @staticmethod
    def _preview_url(item: Any) -> str:
        image_urls = getattr(item, "image_urls", None)
        for key in ("large", "medium", "square_medium"):
            url = getattr(image_urls, key, None)
            if isinstance(url, str) and url:
                return url
        return ""

    async def _collect(self, query: str) -> tuple[list[Any], list[str], str]:
        parsed = validate_and_process_tags(query)
        if not parsed.get("success"):
            return [], [], str(parsed.get("error_message") or "标签无效")

        client = self.controller.client
        pages = self._bounded_int("pixiv_search_pages", 5, 1, 20)
        all_items: list[Any] = []
        next_params: dict[str, Any] | None = None
        for page in range(pages):
            try:
                if page == 0:
                    response = await asyncio.to_thread(
                        client.search_illust,
                        parsed["search_tags"],
                        search_target="partial_match_for_tags",
                        sort="date_desc",
                        filter="for_ios",
                    )
                elif next_params:
                    response = await asyncio.to_thread(
                        client.search_illust, **next_params
                    )
                else:
                    break
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AliceImagePixiv] 第 %s 页搜索失败: %s", page + 1, exc)
                break
            page_items = list(getattr(response, "illusts", None) or [])
            if not page_items:
                break
            all_items.extend(page_items)
            next_url = getattr(response, "next_url", None)
            next_params = client.parse_qs(next_url) if next_url else None
            if next_params:
                await asyncio.sleep(0.15)

        if not all_items:
            return [], parsed.get("exclude_tags", []), "没有找到 Pixiv 候选作品。"
        all_items.sort(
            key=lambda item: getattr(item, "total_bookmarks", 0) or 0,
            reverse=True,
        )
        return all_items, parsed.get("exclude_tags", []), ""

    def _filter(
        self, items: list[Any], excluded_tags: list[str], count: int
    ) -> list[Any]:
        cfg = self.controller.pixiv_config
        filter_cfg = FilterConfig(
            r18_mode=cfg.r18_mode,
            filter_r18g_only=cfg.filter_r18g_only,
            ai_filter_mode=cfg.ai_filter_mode,
            ai_detection_mode=cfg.ai_detection_mode,
            min_bookmarks=cfg.min_bookmarks,
            min_views=cfg.min_views,
            min_likes=cfg.min_likes,
            return_count=count,
            display_tag_str="LLM 精确找图",
            logger=logger,
            show_filter_result=False,
            single_response_mode=cfg.single_response_mode,
            excluded_tags=excluded_tags,
            forward_threshold=cfg.forward_threshold,
            show_details=cfg.show_details,
        )
        filtered, _ = filter_illusts_with_reason(items, filter_cfg)
        return filtered

    async def _review(
        self,
        event: AstrMessageEvent,
        items: list[Any],
        description: str,
        count: int,
    ) -> tuple[list[Any], bool, str]:
        provider = await self._provider(event)
        fail_open = bool(self.review_config.get("fail_open", True))
        if provider is None:
            if fail_open:
                return items[:count], True, ""
            return [], False, "未找到可用的视觉审核模型。"

        candidate_count = self._bounded_int("candidate_count", 12, count, 24)
        session = await self.controller._get_http_session()

        async def fetch(item: Any) -> tuple[str, bytes] | None:
            url = self._preview_url(item)
            if not url:
                return None
            data = await download_image(session, url)
            return (url, data) if data else None

        downloaded = await asyncio.gather(
            *(fetch(item) for item in items[:candidate_count]),
            return_exceptions=True,
        )
        item_by_url = {
            self._preview_url(item): item for item in items[:candidate_count]
        }
        valid = [value for value in downloaded if isinstance(value, tuple)]
        collage_bytes, collage_items = await self._collage.create_collage_from_items(
            valid
        )
        if not collage_bytes or not collage_items:
            if fail_open:
                return items[:count], True, ""
            return [], False, "Pixiv 候选图下载失败，无法执行审核。"

        try:
            async with self._review_lock:
                indices = await select_from_collage(
                    collage_bytes,
                    description,
                    len(collage_items),
                    provider,
                    max_selection=count,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AliceImagePixiv] 视觉审核异常: %s", exc)
            indices = []

        selected = [
            item_by_url.get(collage_items[index - 1][0])
            for index in indices
            if 1 <= index <= len(collage_items)
        ]
        selected = [item for item in selected if item is not None]
        if not selected:
            if fail_open:
                return items[:count], True, ""
            return [], False, "视觉审核没有选出符合描述的 Pixiv 作品。"

        selected_ids = {getattr(item, "id", None) for item in selected}
        selected.extend(
            item for item in items if getattr(item, "id", None) not in selected_ids
        )
        return selected[:count], False, ""

    async def search(
        self,
        event: AstrMessageEvent,
        query: str,
        description: str,
        count: int = 1,
        review_enabled: bool = True,
        send_images: bool = True,
    ) -> PixivForwardResult:
        if not await self.controller.client_wrapper.authenticate():
            return PixivForwardResult(
                error=self.controller.pixiv_config.get_auth_error_message()
            )

        count = max(1, min(int(count), 5))
        items, excluded_tags, error = await self._collect(query)
        if error:
            return PixivForwardResult(error=error)
        filtered = self._filter(items, excluded_tags, count)
        if not filtered:
            return PixivForwardResult(error="候选作品全部被内容或质量规则过滤。")

        selected = filtered[:count]
        review_fallback = False
        if review_enabled and description.strip() and len(filtered) > 1:
            selected, review_fallback, error = await self._review(
                event,
                filtered,
                description.strip(),
                count,
            )
            if error:
                return PixivForwardResult(error=error)

        ids = [int(item.id) for item in selected if getattr(item, "id", None)]
        if not send_images:
            return PixivForwardResult(
                success=True,
                ids=ids,
                sent_count=0,
                review_fallback=review_fallback,
            )

        cfg = self.controller.pixiv_config
        send_cfg = FilterConfig(
            r18_mode=cfg.r18_mode,
            filter_r18g_only=cfg.filter_r18g_only,
            ai_filter_mode=cfg.ai_filter_mode,
            ai_detection_mode=cfg.ai_detection_mode,
            min_bookmarks=cfg.min_bookmarks,
            min_views=cfg.min_views,
            min_likes=cfg.min_likes,
            return_count=len(selected),
            display_tag_str=f"搜索:{query}",
            logger=logger,
            show_filter_result=False,
            single_response_mode=cfg.single_response_mode,
            excluded_tags=excluded_tags,
            forward_threshold=cfg.forward_threshold,
            show_details=cfg.show_details,
        )
        sent = 0
        async for result in process_and_send_illusts_sorted(
            selected,
            send_cfg,
            self.controller.client,
            event,
            build_detail_message,
            send_pixiv_image,
            send_forward_message,
            is_novel=False,
        ):
            try:
                await event.send(result)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AliceImagePixiv] 发送候选失败: %s", exc)

        return PixivForwardResult(
            success=sent > 0,
            error="" if sent > 0 else "找到 Pixiv 作品但发送失败。",
            ids=ids,
            sent_count=sent,
            review_fallback=review_fallback,
        )
