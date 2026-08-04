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
    parse_tags_with_exclusion,
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
    found_count: int = 0
    sent_count: int = 0
    send_attempted: bool = False
    delivery_uncertain: bool = False
    review_fallback: bool = False
    artist_user_id: int | None = None
    artist_name: str = ""
    artist_account: str = ""


@dataclass(slots=True)
class PixivArtistTarget:
    user_id: int
    name: str = ""
    account: str = ""


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
        self._background_send_tasks: set[asyncio.Task[None]] = set()

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.review_config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    async def close(self) -> None:
        for task in list(self._background_send_tasks):
            task.cancel()
        if self._background_send_tasks:
            await asyncio.gather(
                *self._background_send_tasks,
                return_exceptions=True,
            )
            self._background_send_tasks.clear()
        await self._collage.close_all()

    def _track_background_send(self, task: asyncio.Task[None]) -> None:
        self._background_send_tasks.add(task)

        def _done(done_task: asyncio.Task[None]) -> None:
            self._background_send_tasks.discard(done_task)
            if done_task.cancelled():
                return
            try:
                done_task.result()
                logger.info("[AliceImagePixiv] 后台发送任务已完成。")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AliceImagePixiv] 后台发送任务最终失败: %s", exc)

        task.add_done_callback(_done)

    async def _send_with_wait_limit(
        self,
        event: AstrMessageEvent,
        result: Any,
        timeout_seconds: float,
    ) -> tuple[bool, bool, str]:
        if timeout_seconds <= 0:
            try:
                await event.send(result)
                return True, False, ""
            except Exception as exc:  # noqa: BLE001
                return False, True, str(exc)

        task = asyncio.create_task(event.send(result))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_seconds)
            return True, False, ""
        except TimeoutError:
            self._track_background_send(task)
            return (
                False,
                True,
                f"发送等待超过 {timeout_seconds:g} 秒，已转入后台继续等待平台确认",
            )
        except Exception as exc:  # noqa: BLE001
            return False, True, str(exc)

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

    @staticmethod
    def _user_from_preview(preview: Any) -> Any:
        return getattr(preview, "user", preview)

    @staticmethod
    def _user_id(user: Any) -> int | None:
        raw_id = getattr(user, "id", None)
        try:
            return int(raw_id)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean_artist_text(value: Any) -> str:
        return str(value or "").strip().lstrip("@").strip()

    async def _resolve_artist(
        self,
        *,
        artist_name: str = "",
        pixiv_user_id: str | int = "",
    ) -> tuple[PixivArtistTarget | None, str]:
        raw_user_id = str(pixiv_user_id or "").strip()
        if raw_user_id:
            if not raw_user_id.isdigit():
                return None, f"Pixiv 用户 ID 必须是数字：{raw_user_id}"
            user_id = int(raw_user_id)
            try:
                detail = await self.controller.client_wrapper.call_pixiv_api(
                    self.controller.client.user_detail,
                    user_id,
                )
                user = getattr(detail, "user", None)
                if user:
                    return (
                        PixivArtistTarget(
                            user_id=user_id,
                            name=str(getattr(user, "name", "") or ""),
                            account=str(getattr(user, "account", "") or ""),
                        ),
                        "",
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AliceImagePixiv] 获取画师详情失败，继续使用用户 ID %s: %s",
                    user_id,
                    exc,
                )
            return PixivArtistTarget(user_id=user_id, name=f"用户ID {user_id}"), ""

        name = self._clean_artist_text(artist_name)
        if not name:
            return None, "请提供 Pixiv 画师名或用户 ID。"

        try:
            response = await self.controller.client_wrapper.call_pixiv_api(
                self.controller.client.search_user,
                name,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AliceImagePixiv] 搜索画师失败: %s", exc)
            return None, f"搜索 Pixiv 画师失败：{exc}"

        previews = list(getattr(response, "user_previews", None) or [])
        if not previews:
            return None, f"没有找到 Pixiv 画师：{name}"

        target_norm = name.lower()

        def exact_score(preview: Any) -> int:
            user = self._user_from_preview(preview)
            candidates = [
                str(getattr(user, "name", "") or "").strip().lower(),
                str(getattr(user, "account", "") or "").strip().lower(),
                str(getattr(user, "id", "") or "").strip().lower(),
            ]
            if target_norm in candidates:
                return 2
            if any(
                target_norm and target_norm in candidate for candidate in candidates
            ):
                return 1
            return 0

        selected_preview = max(previews, key=exact_score)
        user = self._user_from_preview(selected_preview)
        resolved_user_id = self._user_id(user)
        if resolved_user_id is None:
            return None, f"Pixiv 画师「{name}」缺少有效用户 ID。"
        return (
            PixivArtistTarget(
                user_id=resolved_user_id,
                name=str(getattr(user, "name", "") or name),
                account=str(getattr(user, "account", "") or ""),
            ),
            "",
        )

    @staticmethod
    def _tag_texts(tags: Any) -> list[str]:
        if not tags:
            return []
        if not isinstance(tags, (list, tuple, set)):
            tags = [tags]
        values: list[str] = []
        for tag in tags:
            if isinstance(tag, str):
                values.append(tag)
                continue
            for key in ("name", "translated_name", "translatedName"):
                value = getattr(tag, key, "")
                if value:
                    values.append(str(value))
        return values

    def _artist_item_search_text(self, item: Any) -> str:
        user = getattr(item, "user", None)
        fields = [
            getattr(item, "title", ""),
            getattr(item, "caption", ""),
            getattr(user, "name", ""),
            getattr(user, "account", ""),
            *self._tag_texts(getattr(item, "tags", [])),
        ]
        return "\n".join(str(field).lower() for field in fields if field)

    @staticmethod
    def _expand_query_terms(include_tags: list[str]) -> list[str]:
        terms: list[str] = []
        for tag in include_tags:
            stripped = str(tag or "").strip().lower()
            if not stripped:
                continue
            terms.append(stripped)
            terms.extend(part for part in stripped.split() if part)
        return list(dict.fromkeys(terms))

    def _rank_artist_items(
        self, items: list[Any], include_tags: list[str]
    ) -> list[Any]:
        terms = self._expand_query_terms(include_tags)

        def bookmark_count(item: Any) -> int:
            try:
                return int(getattr(item, "total_bookmarks", 0) or 0)
            except (TypeError, ValueError):
                return 0

        if not terms:
            return sorted(items, key=bookmark_count, reverse=True)

        scored: list[tuple[int, int, Any]] = []
        for item in items:
            text = self._artist_item_search_text(item)
            score = sum(1 for term in terms if term in text)
            if score > 0:
                scored.append((score, bookmark_count(item), item))

        if scored:
            scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
            return [item for _, _, item in scored]

        return sorted(items, key=bookmark_count, reverse=True)

    async def _collect_artist(
        self,
        query: str,
        *,
        artist_name: str = "",
        pixiv_user_id: str | int = "",
    ) -> tuple[list[Any], list[str], str, PixivArtistTarget | None]:
        target, error = await self._resolve_artist(
            artist_name=artist_name,
            pixiv_user_id=pixiv_user_id,
        )
        if error or target is None:
            return [], [], error, target

        include_tags: list[str] = []
        exclude_tags: list[str] = []
        query = str(query or "").strip()
        if query:
            include_tags, exclude_tags, conflicts = parse_tags_with_exclusion(query)
            if conflicts:
                conflict_list = "、".join(conflicts)
                return (
                    [],
                    [],
                    f"标签冲突：以下标签同时出现在包含和排除列表中：{conflict_list}",
                    target,
                )

        pages = self._bounded_int("pixiv_search_pages", 5, 1, 20)
        all_items: list[Any] = []
        next_params: dict[str, Any] | None = None
        client = self.controller.client
        for page in range(pages):
            try:
                if page == 0:
                    response = await self.controller.client_wrapper.call_pixiv_api(
                        client.user_illusts,
                        target.user_id,
                    )
                elif next_params:
                    response = await self.controller.client_wrapper.call_pixiv_api(
                        client.user_illusts,
                        **next_params,
                    )
                else:
                    break
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[AliceImagePixiv] 获取画师 %s 第 %s 页作品失败: %s",
                    target.user_id,
                    page + 1,
                    exc,
                )
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
            artist_label = target.name or f"用户ID {target.user_id}"
            return [], exclude_tags, f"画师「{artist_label}」没有公开插画作品。", target

        return (
            self._rank_artist_items(all_items, include_tags),
            exclude_tags,
            "",
            target,
        )

    def _filter(
        self,
        items: list[Any],
        excluded_tags: list[str],
        count: int,
        display_tag_str: str = "LLM 精确找图",
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
            display_tag_str=display_tag_str,
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
        send_wait_timeout_seconds: float = 0,
        artist_name: str = "",
        pixiv_user_id: str | int = "",
    ) -> PixivForwardResult:
        if not await self.controller.client_wrapper.authenticate():
            return PixivForwardResult(
                error=self.controller.pixiv_config.get_auth_error_message()
            )

        count = max(1, min(int(count), 5))
        artist_target: PixivArtistTarget | None = None
        artist_scoped = bool(
            str(artist_name or "").strip() or str(pixiv_user_id or "").strip()
        )
        if artist_scoped:
            if not self.controller.features.get("artist_search", True):
                return PixivForwardResult(error="Pixiv 指定画师找图功能已关闭。")
            items, excluded_tags, error, artist_target = await self._collect_artist(
                query,
                artist_name=artist_name,
                pixiv_user_id=pixiv_user_id,
            )
        else:
            items, excluded_tags, error = await self._collect(query)
        if error:
            return PixivForwardResult(
                error=error,
                artist_user_id=artist_target.user_id if artist_target else None,
                artist_name=artist_target.name if artist_target else "",
                artist_account=artist_target.account if artist_target else "",
            )

        display_tag_str = (
            f"画师:{artist_target.name or artist_target.user_id}"
            if artist_target
            else "LLM 精确找图"
        )
        filtered = self._filter(items, excluded_tags, count, display_tag_str)
        if not filtered:
            return PixivForwardResult(
                error="候选作品全部被内容或质量规则过滤。",
                artist_user_id=artist_target.user_id if artist_target else None,
                artist_name=artist_target.name if artist_target else "",
                artist_account=artist_target.account if artist_target else "",
            )

        selection_policy = getattr(self.controller, "selection_policy", None)
        candidates = filtered
        if selection_policy is not None:
            candidates = await selection_policy.select(
                event,
                filtered,
                len(filtered),
                remember=False,
                fill_from_history=False,
            )

        selected = candidates[:count]
        review_fallback = False
        if review_enabled and description.strip() and len(candidates) > 1:
            selected, review_fallback, error = await self._review(
                event,
                candidates,
                description.strip(),
                count,
            )
            if error:
                return PixivForwardResult(error=error)

        selected_ids = {getattr(item, "id", None) for item in selected}
        ordered_candidates = selected + [
            item
            for item in candidates
            if getattr(item, "id", None) not in selected_ids
        ]
        if selection_policy is not None:
            selected = await selection_policy.select(
                event,
                ordered_candidates,
                count,
                randomize=False,
                remember=send_images,
            )
        else:
            selected = ordered_candidates[:count]

        ids = [int(item.id) for item in selected if getattr(item, "id", None)]
        if not send_images:
            return PixivForwardResult(
                success=True,
                ids=ids,
                found_count=len(selected),
                sent_count=0,
                send_attempted=False,
                review_fallback=review_fallback,
                artist_user_id=artist_target.user_id if artist_target else None,
                artist_name=artist_target.name if artist_target else "",
                artist_account=artist_target.account if artist_target else "",
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
            display_tag_str=(
                f"画师:{artist_target.name or artist_target.user_id}"
                if artist_target
                else f"搜索:{query}"
            ),
            logger=logger,
            show_filter_result=False,
            single_response_mode=cfg.single_response_mode,
            excluded_tags=excluded_tags,
            forward_threshold=cfg.forward_threshold,
            show_details=cfg.show_details,
        )
        sent = 0
        send_attempted = False
        delivery_uncertain = False
        last_send_error = ""
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
            send_attempted = True
            sent_ok, uncertain, send_error = await self._send_with_wait_limit(
                event,
                result,
                send_wait_timeout_seconds,
            )
            if sent_ok:
                sent += 1
            else:
                delivery_uncertain = True
                if uncertain:
                    last_send_error = send_error
                logger.warning("[AliceImagePixiv] 发送候选失败: %s", send_error)

        if sent > 0:
            error = ""
        elif send_attempted:
            error = "Pixiv 已找到作品并尝试发送，但平台发送确认超时或失败；不会切换其它图源。"
            if last_send_error:
                error = f"{error}最后一次发送错误：{last_send_error}"
        else:
            error = "Pixiv 已找到作品，但没有生成可发送的消息；不会切换其它图源。"

        return PixivForwardResult(
            success=bool(selected),
            error=error,
            ids=ids,
            found_count=len(selected),
            sent_count=sent,
            send_attempted=send_attempted,
            delivery_uncertain=delivery_uncertain,
            review_fallback=review_fallback,
            artist_user_id=artist_target.user_id if artist_target else None,
            artist_name=artist_target.name if artist_target else "",
            artist_account=artist_target.account if artist_target else "",
        )
