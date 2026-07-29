"""爱丽丝的图片助手：文字找图、Pixiv 与以图搜图合集。"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncGenerator
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

from .alice_image.config import NestedConfigProxy, as_dict, section
from .alice_image.forward.orchestrator import ForwardSearchOrchestrator
from .alice_image.forward.pixiv_search import PixivForwardSearchService
from .alice_image.forward.serpapi.service import SerpApiForwardService
from .alice_image.forward.soutu.service import SoutuSearchService
from .alice_image.pixiv.controller import AlicePixivController
from .alice_image.reverse.controller import AliceReverseController
from .alice_image.tools import (
    AliceFindImageTool,
    AlicePixivNovelTool,
    AliceReverseImageTool,
    AliceSessionImagesTool,
)

PLUGIN_ID = "astrbot_plugin_alice_image_assistant"
PLUGIN_NAME = "爱丽丝的图片助手"
PLUGIN_VERSION = "1.0.0"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_alice_image_assistant"


@register(
    PLUGIN_ID,
    "Huli3",
    "让 Bot 自主精确找图、挑图和以图搜图，并保留完整指令入口。",
    PLUGIN_VERSION,
    PLUGIN_REPO,
)
class AliceImageAssistantPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.raw_config = config or {}
        self.config = as_dict(self.raw_config)
        self.find_config = section(self.config, "find_image")
        self.reverse_config = section(self.config, "reverse_image")

        self.pixiv: AlicePixivController | None = None
        self.reverse: AliceReverseController | None = None
        self.forward: ForwardSearchOrchestrator | None = None

        pixiv_forward: PixivForwardSearchService | None = None
        soutu: SoutuSearchService | None = None
        serpapi: SerpApiForwardService | None = None

        if self.find_config.get("enabled", True):
            pixiv_config = section(self.find_config, "pixiv")
            pixiv_features = section(pixiv_config, "features")
            if pixiv_config.get("enabled", True):
                settings = NestedConfigProxy(
                    self.raw_config,
                    ("find_image", "pixiv", "settings"),
                )
                self.pixiv = AlicePixivController(
                    context,
                    settings,
                    features=pixiv_features,
                )
                if pixiv_features.get("llm_search", True):
                    review = section(self.find_config, "llm_review")
                    pixiv_forward = PixivForwardSearchService(
                        context,
                        self.pixiv,
                        review_config=review,
                    )

            soutu_config = section(self.find_config, "soutu")
            if soutu_config.get("enabled", True):
                soutu = SoutuSearchService(context, soutu_config)

            serpapi_config = section(self.find_config, "serpapi")
            if not serpapi_config.get("serpapi_keys"):
                reverse_keys = section(self.reverse_config, "api_keys").get(
                    "serpapi_keys", []
                )
                if reverse_keys:
                    serpapi_config["serpapi_keys"] = reverse_keys
            if serpapi_config.get("enabled", True):
                serpapi = SerpApiForwardService(context, serpapi_config)

            self.forward = ForwardSearchOrchestrator(
                self.find_config,
                pixiv_forward,
                soutu,
                serpapi,
            )

        if self.reverse_config.get("enabled", True):
            reverse_runtime = copy.deepcopy(self.reverse_config)
            api_keys = reverse_runtime.setdefault("api_keys", {})
            if not api_keys.get("serpapi_keys"):
                find_keys = section(self.find_config, "serpapi").get("serpapi_keys", [])
                if find_keys:
                    api_keys["serpapi_keys"] = find_keys
            self.reverse = AliceReverseController(context, reverse_runtime)

        self._register_tools()
        logger.info("[%s] v%s 已加载", PLUGIN_NAME, PLUGIN_VERSION)

    def _register_tools(self) -> None:
        tools = []
        if self.find_config.get("enabled", True) and self.find_config.get(
            "llm_tools_enabled", True
        ):
            tools.append(AliceFindImageTool(plugin=self))
            pixiv_features = section(section(self.find_config, "pixiv"), "features")
            if self.pixiv and pixiv_features.get("novel_tool", True):
                tools.append(AlicePixivNovelTool(plugin=self))

        if self.reverse and self.reverse_config.get("llm_tools_enabled", True):
            if self.reverse_config.get("list_images_tool_enabled", True):
                tools.append(AliceSessionImagesTool(plugin=self))
            if self.reverse_config.get("reverse_tool_enabled", True):
                tools.append(AliceReverseImageTool(plugin=self))

        if tools:
            self.context.add_llm_tools(*tools)
            logger.info(
                "[%s] 已注册工具: %s",
                PLUGIN_NAME,
                ", ".join(tool.name for tool in tools),
            )

    async def terminate(self) -> None:
        if self.forward:
            await self.forward.close()
        if self.reverse:
            await self.reverse.terminate()
        if self.pixiv:
            await self.pixiv.terminate()
        logger.info("[%s] 已卸载并释放网络、浏览器与调度资源", PLUGIN_NAME)

    def _find_commands_enabled(self) -> bool:
        return self.find_config.get("enabled", True) and self.find_config.get(
            "commands_enabled", True
        )

    def _reverse_commands_enabled(self) -> bool:
        return self.reverse_config.get("enabled", True) and self.reverse_config.get(
            "commands_enabled", True
        )

    def _pixiv_capability(self, name: str) -> bool:
        pixiv_config = section(self.find_config, "pixiv")
        features = section(pixiv_config, "features")
        return (
            self.find_config.get("enabled", True)
            and pixiv_config.get("enabled", True)
            and features.get(name, True)
        )

    def _pixiv_feature(self, name: str) -> bool:
        return self._find_commands_enabled() and self._pixiv_capability(name)

    async def _pixiv_results(
        self,
        event: AstrMessageEvent,
        feature: str,
        method: str,
        *args: Any,
    ) -> AsyncGenerator[Any, None]:
        if self.pixiv is None:
            yield event.plain_result("Pixiv 模块未启用或尚未配置。")
            return
        if not self._pixiv_feature(feature):
            yield event.plain_result(f"Pixiv 功能「{feature}」已关闭。")
            return
        handler = getattr(self.pixiv, method)
        async for result in handler(event, *args):
            yield result

    @staticmethod
    def _command_tail(event: AstrMessageEvent) -> str:
        parts = (event.message_str or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    async def _find_command(
        self,
        event: AstrMessageEvent,
        source: str,
    ) -> AsyncGenerator[Any, None]:
        if not self._find_commands_enabled() or self.forward is None:
            yield event.plain_result("找图指令已关闭。")
            return
        query = self._command_tail(event)
        if not query:
            yield event.plain_result("请提供关键词，例如：/爱图找 雪山 日出。")
            return
        yield event.plain_result(f"正在从 {source} 来源寻找「{query}」...")
        outcome = await self.forward.search(
            event,
            query,
            query,
            source=source,
            count=1,
            for_command=True,
        )
        if outcome.success:
            note = "；视觉审核已回退到首选结果" if outcome.review_fallback else ""
            yield event.plain_result(f"找图完成，来源：{outcome.source}{note}。")
        else:
            details = "；".join(
                f"{name}: {message}" for name, message in outcome.errors.items()
            )
            yield event.plain_result(f"找图失败：{details or '所有来源均无结果'}")

    async def tool_find_image(
        self,
        event: AstrMessageEvent,
        query: str,
        description: str,
        source: str,
        count: int,
        is_explanation: bool,
    ) -> str:
        if not self.find_config.get("enabled", True) or not self.find_config.get(
            "llm_tools_enabled", True
        ):
            return json.dumps(
                {"success": False, "error": "找图 LLM 工具已关闭"},
                ensure_ascii=False,
            )
        if is_explanation and not self.find_config.get(
            "auto_illustration_enabled", True
        ):
            return json.dumps(
                {"success": False, "error": "主动配图功能已关闭"},
                ensure_ascii=False,
            )
        if self.forward is None:
            return json.dumps(
                {"success": False, "error": "没有可用找图来源"},
                ensure_ascii=False,
            )
        if self.find_config.get("show_tool_progress", True):
            await event.send(event.plain_result(f"正在为你寻找「{query}」的图片..."))
        outcome = await self.forward.search(
            event,
            query,
            description or query,
            source=source,
            count=count,
            for_command=False,
        )
        return outcome.to_json()

    async def tool_list_session_images(self, event: AstrMessageEvent) -> str:
        if self.reverse is None or not self.reverse_config.get(
            "list_images_tool_enabled", True
        ):
            return json.dumps(
                {"success": False, "error": "会话图片列表工具已关闭"},
                ensure_ascii=False,
            )
        return await self.reverse.tool_get_session_images(event)

    async def tool_reverse_image(
        self,
        event: AstrMessageEvent,
        image_id: str | None,
        image_index: int,
        strategies: str | None,
    ) -> str:
        if self.reverse is None or not self.reverse_config.get(
            "reverse_tool_enabled", True
        ):
            return json.dumps(
                {"success": False, "error": "以图搜图工具已关闭"},
                ensure_ascii=False,
            )
        return await self.reverse.tool_search_image(
            event,
            image_index=image_index,
            strategies=strategies,
            image_id=image_id,
        )

    async def tool_pixiv_novel(
        self,
        event: AstrMessageEvent,
        query: str,
    ) -> str:
        if self.pixiv is None or not self._pixiv_capability("novel_tool"):
            return json.dumps(
                {"success": False, "error": "Pixiv 小说工具已关闭"},
                ensure_ascii=False,
            )
        return await self.pixiv.pixiv_llm_search(
            str(query or "").strip(),
            search_type="novel",
            event=event,
        )

    @filter.on_llm_request()
    async def inject_tool_guidance(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        marker = "【爱丽丝图片工具】"
        if marker in (req.system_prompt or ""):
            return

        guidance_parts: list[str] = []
        find_guidance_enabled = self.find_config.get(
            "enabled", True
        ) and self.find_config.get("inject_tool_guidance_enabled", True)
        if find_guidance_enabled and self.find_config.get("llm_tools_enabled", True):
            find_guidance = (
                "用户明确要求找图、发图、看图或壁纸时，调用 alice_image_find。"
                "source 可用 auto/pixiv/soutu/serpapi；不确定时用 auto。"
                "visual_description 只写真实需要匹配的主体、外观与风格，不要虚构罕见细节。"
            )
            if self.find_config.get("auto_illustration_enabled", True):
                find_guidance += (
                    "介绍明确实体且配图明显有帮助时，可以主动调用 alice_image_find，"
                    "并设置 is_explanation=true。"
                )
            guidance_parts.append(find_guidance)

        reverse_guidance_enabled = (
            self.reverse is not None
            and self.reverse_config.get("llm_tools_enabled", True)
            and self.reverse_config.get("inject_tool_guidance_enabled", True)
            and self.reverse_config.get("reverse_tool_enabled", True)
        )
        if reverse_guidance_enabled:
            if self.reverse_config.get("list_images_tool_enabled", True):
                guidance_parts.append(
                    "用户要求查图片来源时，先调用 alice_image_list_session_images，"
                    "再用 image_id 调用 alice_image_reverse_search。"
                )
            else:
                guidance_parts.append(
                    "用户要求查最近一张图片来源时，直接调用 alice_image_reverse_search；"
                    "需要选择更早的图片时使用 image_index。"
                )

        if guidance_parts:
            guidance = f"\n{marker}\n" + "\n".join(guidance_parts) + "\n"
            req.system_prompt = (req.system_prompt or "") + guidance

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        if self.reverse:
            await self.reverse.on_message(event)
        if self.pixiv and self._pixiv_feature("url_lookup"):
            async for result in self.pixiv.pixiv_url_all(event):
                yield result

    @filter.command("爱图", alias=["alice-image"])
    async def command_help(self, event: AstrMessageEvent):
        find_sources = (
            self.forward.choose_sources("普通照片", "auto") if self.forward else []
        )
        reverse_strategies = (
            self.reverse.service.get_available_strategies() if self.reverse else []
        )
        text = (
            "爱丽丝的图片助手\n"
            f"找图：{'开启' if self.find_config.get('enabled', True) else '关闭'}；"
            f"当前来源：{', '.join(find_sources) or '无'}\n"
            f"以图搜图：{'开启' if self.reverse else '关闭'}；"
            f"当前引擎：{', '.join(reverse_strategies) or '无'}\n\n"
            "常用指令：\n"
            "/爱图找 <关键词>  自动找图\n"
            "/爱图P <标签>    Pixiv 找图\n"
            "/爱图溯 [引擎]   附图、回复图片或随后发图\n"
            "/爱图P帮助       查看 Pixiv 全部指令"
        )
        yield event.plain_result(text)

    @filter.command("爱图找", alias=["alice-find"])
    async def command_find_auto(self, event: AstrMessageEvent):
        async for result in self._find_command(event, "auto"):
            yield result

    @filter.command("爱图神", alias=["alice-soutu"])
    async def command_find_soutu(self, event: AstrMessageEvent):
        async for result in self._find_command(event, "soutu"):
            yield result

    @filter.command("爱图S", alias=["alice-serp"])
    async def command_find_serpapi(self, event: AstrMessageEvent):
        async for result in self._find_command(event, "serpapi"):
            yield result

    @filter.command("爱图溯", alias=["alice-r"])
    async def command_reverse(self, event: AstrMessageEvent):
        if not self._reverse_commands_enabled() or self.reverse is None:
            yield event.plain_result("以图搜图指令已关闭。")
            return
        async for result in self.reverse.search_image_cmd(event):
            yield result

    @filter.command("爱图P", alias=["alice-p"])
    async def pixiv_search(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(
            event, "illust_search", "pixiv_search_illust", tags
        ):
            yield result

    @filter.command("爱图P新")
    async def pixiv_new(
        self,
        event: AstrMessageEvent,
        content_type: str = "illust",
        max_illust_id: str = "",
    ):
        async for result in self._pixiv_results(
            event,
            "illust_new",
            "pixiv_illust_new",
            content_type,
            max_illust_id,
        ):
            yield result

    @filter.command("爱图P荐")
    async def pixiv_recommended(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._pixiv_results(
            event, "illust_recommended", "pixiv_recommended", args
        ):
            yield result

    @filter.command("爱图P并")
    async def pixiv_and(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(event, "illust_and", "pixiv_and", tags):
            yield result

    @filter.command("爱图PID")
    async def pixiv_specific(self, event: AstrMessageEvent, illust_id: str = ""):
        async for result in self._pixiv_results(
            event, "illust_detail", "pixiv_specific", illust_id
        ):
            yield result

    @filter.command("爱图P榜")
    async def pixiv_ranking(
        self,
        event: AstrMessageEvent,
        mode: str = "",
        date: str = "",
    ):
        async for result in self._pixiv_results(
            event, "ranking", "pixiv_ranking", mode, date
        ):
            yield result

    @filter.command("爱图P似")
    async def pixiv_related(self, event: AstrMessageEvent, illust_id: str = ""):
        async for result in self._pixiv_results(
            event, "related", "pixiv_related", illust_id
        ):
            yield result

    @filter.command("爱图P深")
    async def pixiv_deep(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(
            event, "deep_search", "pixiv_deepsearch", tags
        ):
            yield result

    @filter.command("爱图P评")
    async def pixiv_comments(
        self,
        event: AstrMessageEvent,
        illust_id: str = "",
        offset: str = "",
    ):
        async for result in self._pixiv_results(
            event, "illust_comments", "pixiv_illust_comments", illust_id, offset
        ):
            yield result

    @filter.command("爱图P辑")
    async def pixiv_showcase(self, event: AstrMessageEvent, showcase_id: str = ""):
        async for result in self._pixiv_results(
            event, "showcase", "pixiv_showcase_article", showcase_id
        ):
            yield result

    @filter.command("爱图P画师")
    async def pixiv_user_search(self, event: AstrMessageEvent, username: str = ""):
        async for result in self._pixiv_results(
            event, "user_search", "pixiv_user_search", username
        ):
            yield result

    @filter.command("爱图P画师详")
    async def pixiv_user_detail(self, event: AstrMessageEvent, user_id: str = ""):
        async for result in self._pixiv_results(
            event, "user_detail", "pixiv_user_detail", user_id
        ):
            yield result

    @filter.command("爱图P画师作")
    async def pixiv_user_illusts(self, event: AstrMessageEvent, user_id: str = ""):
        async for result in self._pixiv_results(
            event, "user_illusts", "pixiv_user_illusts", user_id
        ):
            yield result

    @filter.command("爱图P文")
    async def pixiv_novel(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(
            event, "novel_search", "pixiv_novel", tags
        ):
            yield result

    @filter.command("爱图P文荐")
    async def pixiv_novel_recommended(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "novel_recommended", "pixiv_novel_recommended"
        ):
            yield result

    @filter.command("爱图P文新")
    async def pixiv_novel_new(self, event: AstrMessageEvent, max_id: str = ""):
        async for result in self._pixiv_results(
            event, "novel_new", "pixiv_novel_new", max_id
        ):
            yield result

    @filter.command("爱图P文系")
    async def pixiv_novel_series(self, event: AstrMessageEvent, series_id: str = ""):
        async for result in self._pixiv_results(
            event, "novel_series", "pixiv_novel_series", series_id
        ):
            yield result

    @filter.command("爱图P文评")
    async def pixiv_novel_comments(
        self,
        event: AstrMessageEvent,
        novel_id: str = "",
        offset: str = "",
    ):
        async for result in self._pixiv_results(
            event, "novel_comments", "pixiv_novel_comments", novel_id, offset
        ):
            yield result

    @filter.command("爱图P文下")
    async def pixiv_novel_download(self, event: AstrMessageEvent, novel_id: str = ""):
        async for result in self._pixiv_results(
            event, "novel_download", "pixiv_novel_download", novel_id
        ):
            yield result

    @filter.command("爱图P订")
    async def pixiv_sub_add(self, event: AstrMessageEvent, artist_id: str = ""):
        async for result in self._pixiv_results(
            event, "subscriptions", "pixiv_subscribe_add", artist_id
        ):
            yield result

    @filter.command("爱图P退")
    async def pixiv_sub_remove(self, event: AstrMessageEvent, artist_id: str = ""):
        async for result in self._pixiv_results(
            event, "subscriptions", "pixiv_subscribe_remove", artist_id
        ):
            yield result

    @filter.command("爱图P订阅")
    async def pixiv_sub_list(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._pixiv_results(
            event, "subscriptions", "pixiv_subscribe_list", args
        ):
            yield result

    @filter.command("爱图P帮助")
    async def pixiv_help(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._pixiv_results(event, "help", "pixiv_help", args):
            yield result

    @filter.command("爱图P随加")
    async def pixiv_random_add(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_add", tags
        ):
            yield result

    @filter.command("爱图P随删")
    async def pixiv_random_del(self, event: AstrMessageEvent, index: str = ""):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_del", index
        ):
            yield result

    @filter.command("爱图P随列")
    async def pixiv_random_list(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_list", args
        ):
            yield result

    @filter.command("爱图P随停")
    async def pixiv_random_suspend(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_suspend"
        ):
            yield result

    @filter.command("爱图P随开")
    async def pixiv_random_resume(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_resume"
        ):
            yield result

    @filter.command("爱图P随态")
    async def pixiv_random_status(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_status"
        ):
            yield result

    @filter.command("爱图P随跑")
    async def pixiv_random_force(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_force"
        ):
            yield result

    @filter.command("爱图P随榜加")
    async def pixiv_random_ranking_add(
        self,
        event: AstrMessageEvent,
        mode: str = "",
        date: str = "",
    ):
        async for result in self._pixiv_results(
            event, "random_ranking", "pixiv_random_ranking_add", mode, date
        ):
            yield result

    @filter.command("爱图P随榜删")
    async def pixiv_random_ranking_del(
        self,
        event: AstrMessageEvent,
        index: str = "",
    ):
        async for result in self._pixiv_results(
            event, "random_ranking", "pixiv_random_ranking_del", index
        ):
            yield result

    @filter.command("爱图P随榜列")
    async def pixiv_random_ranking_list(
        self,
        event: AstrMessageEvent,
        args: str = "",
    ):
        async for result in self._pixiv_results(
            event, "random_ranking", "pixiv_random_ranking_list", args
        ):
            yield result

    @filter.command("爱图P趋势")
    async def pixiv_trending(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "trending_tags", "pixiv_trending_tags"
        ):
            yield result

    @filter.command("爱图PAI")
    async def pixiv_ai_setting(self, event: AstrMessageEvent, setting: str = ""):
        async for result in self._pixiv_results(
            event, "ai_display_setting", "pixiv_ai_show_settings", setting
        ):
            yield result

    @filter.command("爱图P设置")
    async def pixiv_config_command(
        self,
        event: AstrMessageEvent,
        key: str = "",
        value: str = "",
    ):
        async for result in self._pixiv_results(
            event, "runtime_config", "pixiv_config_command", key, value
        ):
            yield result

    @filter.command("爱图P热")
    async def pixiv_hot(
        self,
        event: AstrMessageEvent,
        tag: str = "",
        duration: str = "",
        pages: str = "",
    ):
        async for result in self._pixiv_results(
            event, "hot_search", "pixiv_hot", tag, duration, pages
        ):
            yield result

    @filter.command("爱图F主")
    async def fanbox_creator(
        self,
        event: AstrMessageEvent,
        creator: str = "",
        limit: str = "",
    ):
        async for result in self._pixiv_results(
            event, "fanbox_creator", "pixiv_fanbox_creator", creator, limit
        ):
            yield result

    @filter.command("爱图F帖")
    async def fanbox_post(self, event: AstrMessageEvent, post: str = ""):
        async for result in self._pixiv_results(
            event, "fanbox_post", "pixiv_fanbox_post", post
        ):
            yield result

    @filter.command("爱图F荐")
    async def fanbox_recommended(self, event: AstrMessageEvent, limit: str = "5"):
        async for result in self._pixiv_results(
            event, "fanbox_recommended", "pixiv_fanbox_recommended", limit
        ):
            yield result

    @filter.command("爱图F找")
    async def fanbox_artist(
        self,
        event: AstrMessageEvent,
        keyword: str = "",
        limit: str = "",
    ):
        async for result in self._pixiv_results(
            event, "fanbox_artist", "pixiv_fanbox_artist", keyword, limit
        ):
            yield result
