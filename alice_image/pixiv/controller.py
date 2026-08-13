import asyncio
from typing import Dict, Any
import aiohttp

from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, StarTools
from astrbot.api import logger

from .utils.database import initialize_database
from .utils.subscription import SubscriptionService
from .utils.pixiv_utils import init_pixiv_utils
from .utils.help import init_help_manager, get_help_message
from .utils.llm_tool import create_pixiv_llm_tools
from .utils.selection import PixivSelectionPolicy
from .utils.tag import set_filter_config_source

from .utils.config import PixivConfig, PixivConfigManager

from .core.client import PixivClientWrapper
from .handlers.illust import IllustHandler
from .handlers.user import UserHandler
from .handlers.novel import NovelHandler
from .handlers.subscribe import SubscribeHandler
from .handlers.random_illust import RandomIllustHandler
from .handlers.misc import MiscHandler
from .handlers.fanbox import FanboxHandler


class AlicePixivController:
    """
    AstrBot ??????? Pixiv API ?????
    ???? AstrBot WebUI ?????
    ??:
        /aaP <??1>,<??2>,... [??]  ?? Pixiv ??
        /aaP??                         ??????
    ????????????????? R18 ?????
    """

    def __init__(
        self,
        context: Context,
        config: Dict[str, Any],
        features: Dict[str, Any] | None = None,
    ):
        """??? Pixiv ??"""
        self.context = context
        self.config = config
        self.features = features or {}

        # 1.????????

        self.pixiv_config = PixivConfig(self.config)
        self.config_manager = PixivConfigManager(self.pixiv_config)

        # 2. ???????? (Facade ??????)
        self.client_wrapper = PixivClientWrapper(self.pixiv_config)
        self.client = self.client_wrapper.client_api
        self.selection_policy = PixivSelectionPolicy(self.pixiv_config)

        # 3. ???????? (Handlers)???????
        self.illust_handler = IllustHandler(
            self.client_wrapper,
            self.pixiv_config,
            self.selection_policy,
        )
        self.user_handler = UserHandler(
            self.client_wrapper,
            self.pixiv_config,
            self.selection_policy,
        )
        self.novel_handler = NovelHandler(self.client_wrapper, self.pixiv_config)
        self.subscribe_handler = SubscribeHandler(
            self.client_wrapper, self.pixiv_config
        )
        self.random_illust_handler = RandomIllustHandler(
            self.client_wrapper,
            self.pixiv_config,
            context,
            features=self.features,
        )
        self.misc_handler = MiscHandler(self.client_wrapper, self.pixiv_config)
        self.fanbox_handler = FanboxHandler(self.pixiv_config)

        self._refresh_task: asyncio.Task = None
        self._http_session = None
        self.sub_service = None
        self.random_search_service = None

        # ?? StarTools ????????
        data_dir = (
            StarTools.get_data_dir("astrbot_plugin_alice_image_assistant") / "pixiv"
        )
        self.temp_dir = data_dir / "temp"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # ??? PixivUtils ??
        init_pixiv_utils(self.client, self.pixiv_config, self.temp_dir)
        set_filter_config_source(self.pixiv_config)

        # ??????????
        init_help_manager(data_dir)

        # ??????
        initialize_database()

        # ???????
        logger.info(f"Pixiv ???????{self.pixiv_config.get_config_info()}")

        has_refresh_token = bool(self.pixiv_config.refresh_token)

        # ???????????????????????? Pixiv ?????
        self._refresh_task = (
            self.client_wrapper.start_refresh_task() if has_refresh_token else None
        )

        # ??????
        if (
            self.features.get("subscriptions", True)
            and self.pixiv_config.subscription_enabled
            and has_refresh_token
        ):
            self.sub_service = SubscriptionService(
                self.client_wrapper, self.pixiv_config, context
            )
            self.sub_service.start()
        else:
            logger.info("Pixiv ???????????")

        # ????????
        self.random_search_service = self.random_illust_handler.random_search_service
        random_background_enabled = self.features.get(
            "random_search", True
        ) or self.features.get("random_ranking", True)
        if random_background_enabled and has_refresh_token:
            self.random_search_service.start()
        else:
            logger.info("????????Pixiv ??????????")

        # ???LLM??
        logger.info(
            f"Pixiv ????????LLM???client: {'???' if self.client else '???'}"
        )
        self.llm_tools = create_pixiv_llm_tools(
            self.client, self.pixiv_config, self.client_wrapper
        )
        logger.info("Pixiv ???LLM???????")

        # LLM ??????????????????

    @staticmethod
    def info() -> Dict[str, Any]:
        """???????"""
        return {
            "name": "alice_image_pixiv",
            "author": "Huli3",
            "description": "???????? Pixiv ??",
            "version": "1.4.0",
            "homepage": "https://github.com/Whereis-Alice/astrbot_plugin_alice_image_assistant",
        }

    # --------???

    async def pixiv_search_illust(
        self,
        event: AstrMessageEvent,
        tags: str = "",
        return_count: int | None = None,
    ):
        """?? Pixiv ??????????????????"""
        async for result in self.illust_handler.pixiv_search_illust(
            event, tags, return_count
        ):
            yield result

    async def pixiv_illust_new(
        self,
        event: AstrMessageEvent,
        content_type: str = "illust",
        max_illust_id: str = "",
    ):
        """??????????"""
        async for result in self.illust_handler.pixiv_illust_new(
            event, content_type, max_illust_id
        ):
            yield result

    async def pixiv_recommended(self, event: AstrMessageEvent, args: str = ""):
        """?? Pixiv ????"""
        async for result in self.illust_handler.pixiv_recommended(event, args):
            yield result

    async def pixiv_and(self, event: AstrMessageEvent, tags: str = ""):
        """?? /aaP? ????? AND ??????"""
        async for result in self.illust_handler.pixiv_and(event, tags):
            yield result

    async def pixiv_url_all(self, event: AstrMessageEvent):
        """??url????,?????p?????,?????"""
        async for result in self.illust_handler.pixiv_msg_url(event, event.message_str):
            yield result

    async def pixiv_specific(self, event: AstrMessageEvent, illust_id: str = ""):
        """???? ID ????????"""
        async for result in self.illust_handler.pixiv_specific(event, illust_id):
            yield result

    async def pixiv_ranking(
        self, event: AstrMessageEvent, mode: str = "", date: str = ""
    ):
        """?? Pixiv ?????"""
        args = " ".join([x for x in [mode, date] if x])
        async for result in self.illust_handler.pixiv_ranking(event, args):
            yield result

    async def pixiv_related(self, event: AstrMessageEvent, illust_id: str = ""):
        """??????????????"""
        async for result in self.illust_handler.pixiv_related(event, illust_id):
            yield result

    async def pixiv_deepsearch(self, event: AstrMessageEvent, tags: str):
        """
        ???? Pixiv ?????????????
        ??: /aaP? <??1>,<??2>,...
        ??: ????????? deep_search_depth ????
        """
        async for result in self.illust_handler.pixiv_deepsearch(event, tags):
            yield result

    async def pixiv_illust_comments(
        self, event: AstrMessageEvent, illust_id: str = "", offset: str = ""
    ):
        """?????????"""
        async for result in self.illust_handler.pixiv_illust_comments(
            event, illust_id, offset
        ):
            yield result

    async def pixiv_showcase_article(
        self, event: AstrMessageEvent, showcase_id: str = ""
    ):
        """??????"""
        async for result in self.illust_handler.pixiv_showcase_article(
            event, showcase_id
        ):
            yield result

    # ----???

    async def pixiv_user_search(self, event: AstrMessageEvent, username: str = ""):
        """?? Pixiv ??"""
        async for result in self.user_handler.pixiv_user_search(event, username):
            yield result

    async def pixiv_user_detail(self, event: AstrMessageEvent, user_id: str = ""):
        """?? Pixiv ????"""
        async for result in self.user_handler.pixiv_user_detail(event, user_id):
            yield result

    async def pixiv_user_illusts(
        self,
        event: AstrMessageEvent,
        user_id: str = "",
        return_count: int | None = None,
    ):
        """?????????"""
        async for result in self.user_handler.pixiv_user_illusts(
            event, user_id, return_count
        ):
            yield result

    async def pixiv_user_random(
        self,
        event: AstrMessageEvent,
        user_id: str = "",
        return_count: int | None = None,
    ):
        """????????????"""
        async for result in self.user_handler.pixiv_user_random(
            event, user_id, return_count
        ):
            yield result

    # --------???

    async def pixiv_novel(self, event: AstrMessageEvent, tags: str = ""):
        """?? /aaP? ????? Pixiv ??"""
        async for result in self.novel_handler.pixiv_novel(event, tags):
            yield result

    async def pixiv_novel_recommended(self, event: AstrMessageEvent):
        """?? Pixiv ????"""
        async for result in self.novel_handler.pixiv_novel_recommended(event):
            yield result

    async def pixiv_novel_new(self, event: AstrMessageEvent, max_novel_id: str = ""):
        """????????"""
        async for result in self.novel_handler.pixiv_novel_new(event, max_novel_id):
            yield result

    async def pixiv_novel_series(self, event: AstrMessageEvent, series_id: str = ""):
        """????????"""
        async for result in self.novel_handler.pixiv_novel_series(event, series_id):
            yield result

    async def pixiv_novel_comments(
        self, event: AstrMessageEvent, novel_id: str = "", offset: str = ""
    ):
        """?????????"""
        async for result in self.novel_handler.pixiv_novel_comments(
            event, novel_id, offset
        ):
            yield result

    async def pixiv_novel_download(self, event: AstrMessageEvent, novel_id: str = ""):
        """??ID??Pixiv???pdf??"""
        async for result in self.novel_handler.pixiv_novel_download(event, novel_id):
            yield result

    # ----???

    async def pixiv_subscribe_add(self, event: AstrMessageEvent, artist_id: str = ""):
        """????"""
        async for result in self.subscribe_handler.pixiv_subscribe_add(
            event, artist_id
        ):
            yield result

    async def pixiv_subscribe_remove(
        self, event: AstrMessageEvent, artist_id: str = ""
    ):
        """??????"""
        async for result in self.subscribe_handler.pixiv_subscribe_remove(
            event, artist_id
        ):
            yield result

    async def pixiv_subscribe_list(self, event: AstrMessageEvent, args: str = ""):
        """????????"""
        async for result in self.subscribe_handler.pixiv_subscribe_list(event, args):
            yield result

    async def pixiv_help(self, event: AstrMessageEvent, args: str = ""):
        """?????????"""

        help_text = get_help_message("pixiv_help", "?????????????????")
        help_text = help_text.replace(
            "`/aaP??? <??ID>`", "`/aaP??? <??ID> [??]`"
        )
        help_text += (
            "\n\n## ??????\n"
            "- `/aaP??? <??ID> [??]` - ???????????????"
            "??????????"
        )
        yield event.plain_result(help_text)

    # ----?????

    async def pixiv_random_add(self, event: AstrMessageEvent, tags: str = ""):
        """????????"""
        async for result in self.random_illust_handler.pixiv_random_add(event, tags):
            yield result

    async def pixiv_random_del(self, event: AstrMessageEvent, index: str = ""):
        """????????"""
        async for result in self.random_illust_handler.pixiv_random_del(event, index):
            yield result

    async def pixiv_random_list(self, event: AstrMessageEvent, args: str = ""):
        """??????/?????????"""
        async for result in self.random_illust_handler.pixiv_random_list(event, args):
            yield result

    async def pixiv_random_suspend(self, event: AstrMessageEvent):
        """?????????????"""
        async for result in self.random_illust_handler.pixiv_random_suspend(event):
            yield result

    async def pixiv_random_resume(self, event: AstrMessageEvent):
        """?????????????"""
        async for result in self.random_illust_handler.pixiv_random_resume(event):
            yield result

    async def pixiv_random_status(self, event: AstrMessageEvent):
        """??????????"""
        async for result in self.random_illust_handler.pixiv_random_status(event):
            yield result

    async def pixiv_random_force(self, event: AstrMessageEvent):
        """??????????????????"""
        async for result in self.random_illust_handler.pixiv_random_force(event):
            yield result

    async def pixiv_random_ranking_add(
        self, event: AstrMessageEvent, mode: str = "", date: str = ""
    ):
        """?????????"""
        args = " ".join([x for x in [mode, date] if x])
        async for result in self.random_illust_handler.pixiv_random_ranking_add(
            event, args
        ):
            yield result

    async def pixiv_random_ranking_del(self, event: AstrMessageEvent, index: str = ""):
        """?????????"""
        async for result in self.random_illust_handler.pixiv_random_ranking_del(
            event, index
        ):
            yield result

    async def pixiv_random_ranking_list(self, event: AstrMessageEvent, args: str = ""):
        """??????????????"""
        async for result in self.random_illust_handler.pixiv_random_ranking_list(
            event, args
        ):
            yield result

    # ----???
    async def pixiv_trending_tags(self, event: AstrMessageEvent):
        """?? Pixiv ??????"""
        async for result in self.misc_handler.pixiv_trending_tags(event):
            yield result

    async def pixiv_ai_show_settings(self, event: AstrMessageEvent, setting: str = ""):
        """??????AI????"""
        async for result in self.misc_handler.pixiv_ai_show_settings(event, setting):
            yield result

    async def pixiv_config_command(
        self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""
    ):
        """??????? Pixiv ?????? refresh_token??"""
        # ???????????
        result = await self.config_manager.handle_config_command(event, arg1, arg2)
        if result:
            yield event.plain_result(result)

    async def pixiv_hot(
        self,
        event: AstrMessageEvent,
        tag: str = "",
        duration: str = "",
        pages: str = "",
    ):
        """????????????"""
        async for result in self.illust_handler.pixiv_hot(event, tag, duration, pages):
            yield result

    async def pixiv_fanbox_creator(
        self,
        event: AstrMessageEvent,
        creator_input: str = "",
        limit: str = "",
    ):
        """?? Fanbox ??????????"""
        args = " ".join([x for x in [creator_input, limit] if x])
        async for result in self.fanbox_handler.pixiv_fanbox_creator(event, args):
            yield result

    async def pixiv_fanbox_post(self, event: AstrMessageEvent, args: str = ""):
        """?? Fanbox ????"""
        async for result in self.fanbox_handler.pixiv_fanbox_post(event, args):
            yield result

    async def pixiv_fanbox_recommended(self, event: AstrMessageEvent, args: str = "5"):
        """?? Fanbox ?????"""
        async for result in self.fanbox_handler.pixiv_fanbox_recommended(event, args):
            yield result

    async def pixiv_fanbox_artist(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        limit: str = "",
    ):
        """? Nekohouse artists ?? Fanbox ???"""
        args = " ".join([x for x in [keyword, limit] if x])
        async for result in self.fanbox_handler.pixiv_fanbox_artist(event, args):
            yield result

    async def terminate(self):
        """????????????"""
        logger.info("Pixiv ????????...")
        # ??????
        if self.sub_service:
            self.sub_service.stop()
        # ????????
        if self.random_search_service:
            await self.random_search_service.stop()
        # ????????
        await self.client_wrapper.stop_refresh_task()
        self._refresh_task = self.client_wrapper._refresh_task

        logger.info("Pixiv ????????")
        # ??HTTP??
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()

    async def _get_http_session(self):
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        return self._http_session

    async def pixiv_llm_search(
        self,
        query: str,
        search_type: str = "illust",
        event: AstrMessageEvent | None = None,
    ) -> str:
        """
        ??LLM????????

        Args:
            query: ??????????????
            search_type: ?????? 'illust', 'novel', 'user' ?

        Returns:
            str: ????
        """
        try:
            # ???????
            if not await self.client_wrapper.authenticate():
                return self.pixiv_config.get_auth_error_message()

            logger.info(
                f"Pixiv ?????LLM???? - ??: {query}, ??: {search_type}"
            )

            # ??PixivSearchTool????
            normalized_type = (search_type or "illust").strip().lower()
            target_tool_name = (
                "pixiv_search_novel"
                if normalized_type in {"novel", "??"}
                else "pixiv_search_illust"
            )

            search_tool = None
            for tool in self.llm_tools:
                if hasattr(tool, "name") and tool.name == target_tool_name:
                    search_tool = tool
                    break

            if not search_tool:
                return "LLM????????"

            if event is None:
                return "?????????????? Pixiv ???"

            # ???????????? AstrAgentContext??????????
            # ?????????????????????????????
            from types import SimpleNamespace

            tool_context = SimpleNamespace(
                event=event,
                context=SimpleNamespace(event=event),
            )
            if normalized_type in {"novel", "??"}:
                result = await search_tool._search_novel(
                    query.strip(), query, tool_context
                )
            else:
                result = await search_tool._search_illust(
                    query.strip(), query, tool_context, 1
                )

            logger.info("Pixiv ???LLM????")
            return result

        except Exception as e:
            error_msg = f"LLM???????: {str(e)}"
            logger.error(error_msg)
            return error_msg
