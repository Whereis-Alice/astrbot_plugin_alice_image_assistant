import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from ..utils.pixiv_utils import (
    filter_items,
    send_pixiv_image,
    send_forward_message,
)
from ..utils.help import get_help_message
from ..utils.tag import (
    build_detail_message,
    FilterConfig,
    item_has_any_exact_tag,
    process_and_send_illusts,
)


class UserHandler:
    """
    Pixiv 用户功能处理器
    负责处理用户搜索、用户详情和用户作品获取功能
    """

    def __init__(self, client_wrapper, pixiv_config):
        self.client_wrapper = client_wrapper
        self.client = client_wrapper.client_api
        self.pixiv_config = pixiv_config

    async def _get_user_name(self, user_id: int) -> str:
        try:
            result = await self.client_wrapper.call_pixiv_api(
                self.client.user_detail, user_id
            )
            user = getattr(result, "user", None)
            name = str(getattr(user, "name", "") or "").strip()
            if name:
                return name
        except Exception as exc:
            logger.warning("Pixiv 插件：获取画师 %s 名称失败: %s", user_id, exc)
        return f"用户ID {user_id}"

    async def _collect_user_illusts(
        self,
        user_id: int,
        *,
        max_pages: int = 1,
    ) -> list:
        items = []
        seen_ids = set()
        next_params = None

        for page_index in range(max(1, max_pages)):
            try:
                if page_index == 0:
                    result = await self.client_wrapper.call_pixiv_api(
                        self.client.user_illusts, user_id
                    )
                elif next_params:
                    result = await self.client_wrapper.call_pixiv_api(
                        self.client.user_illusts, **next_params
                    )
                else:
                    break
            except Exception as exc:
                if not items:
                    raise
                logger.warning(
                    "Pixiv 插件：画师 %s 第 %s 页作品获取失败，使用已获取的 %s 个作品: %s",
                    user_id,
                    page_index + 1,
                    len(items),
                    exc,
                )
                break

            page_items = list(getattr(result, "illusts", None) or [])
            if not page_items:
                break

            for item in page_items:
                raw_id = getattr(item, "id", None)
                item_key = str(raw_id) if raw_id is not None else f"object:{id(item)}"
                if item_key in seen_ids:
                    continue
                seen_ids.add(item_key)
                items.append(item)

            next_url = getattr(result, "next_url", None)
            next_params = self.client.parse_qs(next_url) if next_url else None
            if not next_params:
                break
            await asyncio.sleep(0.15)

        return items

    def _build_user_filter_config(
        self,
        user_name: str,
        return_count: int | None,
        *,
        random_mode: bool = False,
    ) -> FilterConfig:
        label = f"画师随机:{user_name}" if random_mode else f"用户:{user_name}"
        return FilterConfig(
            r18_mode=self.pixiv_config.r18_mode,
            filter_r18g_only=self.pixiv_config.filter_r18g_only,
            ai_filter_mode=self.pixiv_config.ai_filter_mode,
            ai_detection_mode=self.pixiv_config.ai_detection_mode,
            display_tag_str=label,
            return_count=self.pixiv_config.resolve_return_count(return_count),
            logger=logger,
            show_filter_result=self.pixiv_config.show_filter_result,
            single_response_mode=self.pixiv_config.single_response_mode,
            excluded_tags=[],
            forward_threshold=self.pixiv_config.forward_threshold,
            show_details=self.pixiv_config.show_details,
        )

    async def pixiv_user_search(self, event: AstrMessageEvent, username: str = ""):
        """搜索 Pixiv 用户"""
        # 检查参数是否为空或为 help
        if not username.strip() or username.strip().lower() == "help":
            help_text = get_help_message(
                "pixiv_user_search", "用户搜索帮助消息加载失败，请检查配置文件。"
            )
            yield event.plain_result(help_text)
            return

        logger.info(f"Pixiv 插件：正在搜索用户 - {username}")

        # 验证是否已认证
        if not await self.client_wrapper.authenticate():
            yield event.plain_result(self.pixiv_config.get_auth_error_message())
            return

        try:
            # 调用 Pixiv API 搜索用户
            json_result = await self.client_wrapper.call_pixiv_api(
                self.client.search_user, username
            )
            if (
                not json_result
                or not hasattr(json_result, "user_previews")
                or not json_result.user_previews
            ):
                yield event.plain_result(f"未找到用户: {username}")
                return

            # 获取第一个用户
            user_preview = json_result.user_previews[0]
            user = user_preview.user

            # 构建用户信息
            user_info = f"用户名: {user.name}\n"
            user_info += f"用户ID: {user.id}\n"
            user_info += f"账号: @{user.account}\n"
            user_info += f"个人主页: https://www.pixiv.net/users/{user.id}"

            # 如果有作品，统一用 filter_illusts_with_reason 过滤预览插画
            illusts = (
                user_preview.illusts
                if hasattr(user_preview, "illusts") and user_preview.illusts
                else []
            )
            config = FilterConfig(
                r18_mode=self.pixiv_config.r18_mode,
                filter_r18g_only=self.pixiv_config.filter_r18g_only,
                ai_filter_mode=self.pixiv_config.ai_filter_mode,
                ai_detection_mode=self.pixiv_config.ai_detection_mode,
                display_tag_str=f"用户:{user.name}",
                return_count=self.pixiv_config.return_count,
                logger=logger,
                show_filter_result=self.pixiv_config.show_filter_result,
                single_response_mode=self.pixiv_config.single_response_mode,
                excluded_tags=[],
            )
            filtered_illusts, filter_msgs = filter_items(illusts, config)
            if self.pixiv_config.show_filter_result:
                for msg in filter_msgs:
                    yield event.plain_result(msg)

            # 始终显示用户基本信息
            yield event.plain_result(user_info)

            # 如果有合规插画，发送第一张插画
            if filtered_illusts:
                illust = filtered_illusts[0]
                detail_message = build_detail_message(illust, is_novel=False)
                async for result in send_pixiv_image(
                    self.client,
                    event,
                    illust,
                    detail_message,
                    show_details=self.pixiv_config.show_details,
                ):
                    yield result

        except Exception as e:
            logger.error(f"Pixiv 插件：搜索用户时发生错误 - {e}")
            yield event.plain_result(f"搜索用户时发生错误: {str(e)}")

    async def pixiv_user_detail(self, event: AstrMessageEvent, user_id: str = ""):
        """获取 Pixiv 用户详情"""
        # 检查参数是否为空或为 help
        if not user_id.strip() or user_id.strip().lower() == "help":
            help_text = get_help_message(
                "pixiv_user_detail", "用户详情帮助消息加载失败，请检查配置文件。"
            )
            yield event.plain_result(help_text)
            return

        logger.info(f"Pixiv 插件：正在获取用户详情 - ID: {user_id}")

        # 验证用户ID是否为数字
        if not user_id.isdigit():
            yield event.plain_result(f"用户ID必须是数字: {user_id}")
            return

        # 验证是否已认证
        if not await self.client_wrapper.authenticate():
            yield event.plain_result(self.pixiv_config.get_auth_error_message())
            return

        try:
            # 调用 Pixiv API 获取用户详情
            json_result = await self.client_wrapper.call_pixiv_api(
                self.client.user_detail, user_id
            )
            if not json_result or not hasattr(json_result, "user"):
                yield event.plain_result(f"未找到用户 - ID: {user_id}")
                return

            user = json_result.user
            profile = json_result.profile if hasattr(json_result, "profile") else None

            # 构建用户详情信息
            detail_info = f"用户名: {user.name}\n"
            detail_info += f"用户ID: {user.id}\n"
            detail_info += f"账号: @{user.account}\n"

            if profile:
                detail_info += f"地区: {profile.region if hasattr(profile, 'region') else '未知'}\n"
                detail_info += f"生日: {profile.birth_day if hasattr(profile, 'birth_day') else '未知'}\n"
                detail_info += f"性别: {profile.gender if hasattr(profile, 'gender') else '未知'}\n"
                detail_info += f"插画数: {profile.total_illusts if hasattr(profile, 'total_illusts') else '未知'}\n"
                detail_info += f"漫画数: {profile.total_manga if hasattr(profile, 'total_manga') else '未知'}\n"
                detail_info += f"小说数: {profile.total_novels if hasattr(profile, 'total_novels') else '未知'}\n"
                detail_info += f"收藏数: {profile.total_illust_bookmarks_public if hasattr(profile, 'total_illust_bookmarks_public') else '未知'}\n"

            detail_info += (
                f"简介: {user.comment if hasattr(user, 'comment') else '无'}\n"
            )
            detail_info += f"个人主页: https://www.pixiv.net/users/{user.id}"

            # 返回用户详情
            yield event.plain_result(detail_info)

        except Exception as e:
            logger.error(f"Pixiv 插件：获取用户详情时发生错误 - {e}")
            yield event.plain_result(f"获取用户详情时发生错误: {str(e)}")

    async def pixiv_user_illusts(
        self,
        event: AstrMessageEvent,
        user_id: str = "",
        return_count: int | None = None,
    ):
        """获取指定用户的作品"""
        # 检查参数是否为空或为 help
        if not user_id.strip() or user_id.strip().lower() == "help":
            help_text = get_help_message(
                "pixiv_user_illusts", "用户作品帮助消息加载失败，请检查配置文件。"
            )
            help_text = help_text.replace(
                "`/aaP画师作 <用户ID>`", "`/aaP画师作 <用户ID> [数量]`"
            )
            yield event.plain_result(help_text)
            return

        logger.info(f"Pixiv 插件：正在获取用户作品 - ID: {user_id}")

        # 验证用户ID是否为数字
        if not user_id.isdigit():
            yield event.plain_result(f"用户ID必须是数字: {user_id}")
            return

        # 验证是否已认证
        if not await self.client_wrapper.authenticate():
            yield event.plain_result(self.pixiv_config.get_auth_error_message())
            return

        try:
            numeric_user_id = int(user_id)
            user_name = await self._get_user_name(numeric_user_id)
            initial_illusts = await self._collect_user_illusts(numeric_user_id)

            if not initial_illusts:
                yield event.plain_result(
                    f"用户 {user_name} ({user_id}) 没有公开的作品。"
                )
                return

            config = self._build_user_filter_config(user_name, return_count)

            async for result in process_and_send_illusts(
                initial_illusts,  # 传入所有初始作品，让process_and_send_illusts内部处理过滤和选择
                config,
                self.client,
                event,
                build_detail_message,
                send_pixiv_image,
                send_forward_message,
                is_novel=False,
            ):
                yield result

        except Exception as e:
            logger.error(f"Pixiv 插件：获取用户作品时发生错误 - {e}")
            yield event.plain_result(f"获取用户作品时发生错误: {str(e)}")

    async def pixiv_user_random(
        self,
        event: AstrMessageEvent,
        user_id: str = "",
        return_count: int | None = None,
    ):
        """从指定画师的近期作品池中随机发送作品。"""
        if not user_id.strip() or user_id.strip().lower() == "help":
            yield event.plain_result(
                "用法: /aaP画师随 <用户ID> [数量]\n"
                "示例: /aaP画师随 29872901 1\n"
                "数量省略时使用配置中的指令默认返回作品数。"
            )
            return

        if not user_id.isdigit():
            yield event.plain_result(f"用户ID必须是数字: {user_id}")
            return

        if not await self.client_wrapper.authenticate():
            yield event.plain_result(self.pixiv_config.get_auth_error_message())
            return

        numeric_user_id = int(user_id)
        try:
            user_name = await self._get_user_name(numeric_user_id)
            initial_illusts = await self._collect_user_illusts(
                numeric_user_id,
                max_pages=self.pixiv_config.artist_random_pages,
            )
            if not initial_illusts:
                yield event.plain_result(
                    f"用户 {user_name} ({user_id}) 没有公开的作品。"
                )
                return

            blocked_tags = self.pixiv_config.artist_random_blocked_tags
            eligible_illusts = [
                item
                for item in initial_illusts
                if not item_has_any_exact_tag(item, blocked_tags)
            ]
            blocked_count = len(initial_illusts) - len(eligible_illusts)
            if blocked_count and self.pixiv_config.show_filter_result:
                yield event.plain_result(
                    f"画师随机作品池已按屏蔽标签过滤 {blocked_count} 个作品。"
                )

            if not eligible_illusts:
                blocked_display = "、".join(blocked_tags) or "当前配置"
                yield event.plain_result(
                    f"画师 {user_name} 的作品均命中随机屏蔽标签：{blocked_display}。"
                )
                return

            config = self._build_user_filter_config(
                user_name,
                return_count,
                random_mode=True,
            )
            async for result in process_and_send_illusts(
                eligible_illusts,
                config,
                self.client,
                event,
                build_detail_message,
                send_pixiv_image,
                send_forward_message,
                is_novel=False,
            ):
                yield result
        except Exception as e:
            logger.error(f"Pixiv 插件：获取画师随机作品时发生错误 - {e}")
            yield event.plain_result(f"获取画师随机作品时发生错误: {str(e)}")
