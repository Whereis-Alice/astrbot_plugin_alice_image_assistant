"""??????????????Pixiv ????????"""

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
PLUGIN_NAME = "????????"
PLUGIN_VERSION = "1.4.0"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_alice_image_assistant"
MAX_COMMAND_RETURN_COUNT = 10


@register(
    PLUGIN_ID,
    "Whereis-Alice",
    "? Bot ?????????????????????????",
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
        logger.info("[%s] v%s ???", PLUGIN_NAME, PLUGIN_VERSION)

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
                "[%s] ?????: %s",
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
        logger.info("[%s] ?????????????????", PLUGIN_NAME)

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
            yield event.plain_result("Pixiv ???????????")
            return
        if not self._pixiv_feature(feature):
            yield event.plain_result(f"Pixiv ???{feature}?????")
            return
        handler = getattr(self.pixiv, method)
        async for result in handler(event, *args):
            yield result

    @staticmethod
    def _command_tail(event: AstrMessageEvent) -> str:
        parts = (event.message_str or "").strip().split(maxsplit=1)
        return parts[1].strip() if len(parts) > 1 else ""

    @staticmethod
    def _parse_query_count(text: str) -> tuple[str, int | None, str]:
        """Parse an optional trailing image count without breaking multi-word queries."""
        cleaned = str(text or "").strip()
        parts = cleaned.rsplit(maxsplit=1)
        if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdigit():
            return cleaned, None, ""

        count = int(parts[1])
        if not 1 <= count <= MAX_COMMAND_RETURN_COUNT:
            return (
                parts[0].strip(),
                None,
                f"???????? 1-{MAX_COMMAND_RETURN_COUNT} ????",
            )
        return parts[0].strip(), count, ""

    async def _find_command(
        self,
        event: AstrMessageEvent,
        source: str,
    ) -> AsyncGenerator[Any, None]:
        if not self._find_commands_enabled() or self.forward is None:
            yield event.plain_result("????????")
            return
        query = self._command_tail(event)
        if not query:
            yield event.plain_result("??????????/aa? ?? ???")
            return
        yield event.plain_result(f"??? {source} ?????{query}?...")
        outcome = await self.forward.search(
            event,
            query,
            query,
            source=source,
            count=1,
            for_command=True,
        )
        if outcome.success:
            notes = []
            if outcome.review_fallback:
                notes.append("????????????")
            if outcome.delivery_uncertain:
                notes.append("?????????????????????")
            note = f"?{'?'.join(notes)}" if notes else ""
            yield event.plain_result(f"????????{outcome.source}{note}?")
        else:
            details = "?".join(
                f"{name}: {message}" for name, message in outcome.errors.items()
            )
            yield event.plain_result(f"?????{details or '????????'}")

    async def _pixiv_artist_find_command(
        self,
        event: AstrMessageEvent,
    ) -> AsyncGenerator[Any, None]:
        if not self._find_commands_enabled() or self.forward is None:
            yield event.plain_result("????????")
            return
        if not self._pixiv_feature("artist_search"):
            yield event.plain_result("Pixiv ????????????")
            return

        text, count, count_error = self._parse_query_count(self._command_tail(event))
        if count_error:
            yield event.plain_result(count_error)
            return
        if not text:
            yield event.plain_result(
                "???????????????/aaP??? ??? | ???? 1?"
            )
            return
        if "|" in text:
            artist_part, query = (part.strip() for part in text.split("|", 1))
        elif "?" in text:
            artist_part, query = (part.strip() for part in text.split("?", 1))
        else:
            artist_part, query = text.strip(), ""
        if not artist_part:
            yield event.plain_result(
                "??????? Pixiv ?? ID????/aaP??? 12345678 | ???? 1?"
            )
            return

        pixiv_user_id = artist_part if artist_part.isdigit() else ""
        artist_name = "" if pixiv_user_id else artist_part
        description = " ".join(part for part in (artist_part, query) if part)
        label = f"{artist_part} ? {query}" if query else artist_part
        yield event.plain_result(f"??? Pixiv ????????{label}?...")
        outcome = await self.forward.search(
            event,
            query,
            description,
            source="pixiv",
            count=count or self.pixiv.pixiv_config.return_count,
            for_command=True,
            artist_name=artist_name,
            pixiv_user_id=pixiv_user_id,
        )
        if outcome.success:
            artist = outcome.pixiv_artist_name or artist_part
            yield event.plain_result(f"?????Pixiv ???{artist}?")
        else:
            details = "?".join(
                f"{name}: {message}" for name, message in outcome.errors.items()
            )
            yield event.plain_result(f"???????{details or '?????????'}")

    async def tool_find_image(
        self,
        event: AstrMessageEvent,
        query: str,
        description: str,
        source: str,
        count: int,
        is_explanation: bool,
        artist_name: str = "",
        pixiv_user_id: str = "",
    ) -> str:
        if not self.find_config.get("enabled", True) or not self.find_config.get(
            "llm_tools_enabled", True
        ):
            return json.dumps(
                {"success": False, "error": "?? LLM ?????"},
                ensure_ascii=False,
            )
        if is_explanation and not self.find_config.get(
            "auto_illustration_enabled", True
        ):
            return json.dumps(
                {"success": False, "error": "?????????"},
                ensure_ascii=False,
            )
        if self.forward is None:
            return json.dumps(
                {"success": False, "error": "????????"},
                ensure_ascii=False,
            )
        if self.find_config.get("llm_search_progress_message_enabled", False):
            await event.send(event.plain_result(f"???????{query}????..."))
        outcome = await self.forward.search(
            event,
            query,
            description or query,
            source=source,
            count=count,
            for_command=False,
            artist_name=artist_name,
            pixiv_user_id=pixiv_user_id,
        )
        return outcome.to_json()

    async def tool_list_session_images(self, event: AstrMessageEvent) -> str:
        if self.reverse is None or not self.reverse_config.get(
            "list_images_tool_enabled", True
        ):
            return json.dumps(
                {"success": False, "error": "???????????"},
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
                {"success": False, "error": "?????????"},
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
                {"success": False, "error": "Pixiv ???????"},
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
        marker = "?????????"
        if marker in (req.system_prompt or ""):
            return

        guidance_parts: list[str] = []
        find_guidance_enabled = self.find_config.get(
            "enabled", True
        ) and self.find_config.get("inject_tool_guidance_enabled", True)
        if find_guidance_enabled and self.find_config.get("llm_tools_enabled", True):
            find_guidance = (
                "????????????????????? alice_image_find?"
                "source ?? auto/pixiv/soutu/serpapi?????? auto?"
                "???? Pixiv ??/?????? artist_name ? pixiv_user_id?"
                "???????????????????"
                "visual_description ???????????????????????????"
            )
            if self.find_config.get("auto_illustration_enabled", True):
                find_guidance += (
                    "?????????????????????? alice_image_find?"
                    "??? is_explanation=true?"
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
                    "?????????????? alice_image_list_session_images?"
                    "?? image_id ?? alice_image_reverse_search?"
                )
            else:
                guidance_parts.append(
                    "??????????????????? alice_image_reverse_search?"
                    "???????????? image_index?"
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

    @filter.command("aa")
    async def command_help(self, event: AstrMessageEvent):
        find_sources = (
            self.forward.choose_sources("????", "auto") if self.forward else []
        )
        reverse_strategies = (
            self.reverse.service.get_available_strategies() if self.reverse else []
        )
        text = (
            "????????\n"
            f"???{'??' if self.find_config.get('enabled', True) else '??'}?"
            f"?????{', '.join(find_sources) or '?'}\n"
            f"?????{'??' if self.reverse else '??'}?"
            f"?????{', '.join(reverse_strategies) or '?'}\n\n"
            "?????\n"
            "/aa? <???>  ????\n"
            "/aaP <??> [??]  Pixiv ??\n"
            "/aaP??? <??>|<???> [??]  ??????\n"
            "/aa? [??]   ????????????\n"
            "/aaP??       ?? Pixiv ????"
        )
        yield event.plain_result(text)

    @filter.command("aa?")
    async def command_find_auto(self, event: AstrMessageEvent):
        async for result in self._find_command(event, "auto"):
            yield result

    @filter.command("aa?")
    async def command_find_soutu(self, event: AstrMessageEvent):
        async for result in self._find_command(event, "soutu"):
            yield result

    @filter.command("aaS")
    async def command_find_serpapi(self, event: AstrMessageEvent):
        async for result in self._find_command(event, "serpapi"):
            yield result

    @filter.command("aa?")
    async def command_reverse(self, event: AstrMessageEvent):
        if not self._reverse_commands_enabled() or self.reverse is None:
            yield event.plain_result("??????????")
            return
        async for result in self.reverse.search_image_cmd(event):
            yield result

    @filter.command("aaP")
    async def pixiv_search(self, event: AstrMessageEvent):
        tags, return_count, count_error = self._parse_query_count(
            self._command_tail(event)
        )
        if count_error:
            yield event.plain_result(count_error)
            return
        async for result in self._pixiv_results(
            event,
            "illust_search",
            "pixiv_search_illust",
            tags,
            return_count,
        ):
            yield result

    @filter.command("aaP?")
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

    @filter.command("aaP?")
    async def pixiv_recommended(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._pixiv_results(
            event, "illust_recommended", "pixiv_recommended", args
        ):
            yield result

    @filter.command("aaP?")
    async def pixiv_and(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(event, "illust_and", "pixiv_and", tags):
            yield result

    @filter.command("aaPID")
    async def pixiv_specific(self, event: AstrMessageEvent, illust_id: str = ""):
        async for result in self._pixiv_results(
            event, "illust_detail", "pixiv_specific", illust_id
        ):
            yield result

    @filter.command("aaP?")
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

    @filter.command("aaP?")
    async def pixiv_related(self, event: AstrMessageEvent, illust_id: str = ""):
        async for result in self._pixiv_results(
            event, "related", "pixiv_related", illust_id
        ):
            yield result

    @filter.command("aaP?")
    async def pixiv_deep(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(
            event, "deep_search", "pixiv_deepsearch", tags
        ):
            yield result

    @filter.command("aaP?")
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

    @filter.command("aaP?")
    async def pixiv_showcase(self, event: AstrMessageEvent, showcase_id: str = ""):
        async for result in self._pixiv_results(
            event, "showcase", "pixiv_showcase_article", showcase_id
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_user_search(self, event: AstrMessageEvent, username: str = ""):
        async for result in self._pixiv_results(
            event, "user_search", "pixiv_user_search", username
        ):
            yield result

    @filter.command("aaP???")
    async def pixiv_user_detail(self, event: AstrMessageEvent, user_id: str = ""):
        async for result in self._pixiv_results(
            event, "user_detail", "pixiv_user_detail", user_id
        ):
            yield result

    @filter.command("aaP???")
    async def pixiv_user_illusts(self, event: AstrMessageEvent):
        user_id, return_count, count_error = self._parse_query_count(
            self._command_tail(event)
        )
        if count_error:
            yield event.plain_result(count_error)
            return
        async for result in self._pixiv_results(
            event,
            "user_illusts",
            "pixiv_user_illusts",
            user_id,
            return_count,
        ):
            yield result

    @filter.command("aaP???")
    async def pixiv_user_random(self, event: AstrMessageEvent):
        user_id, return_count, count_error = self._parse_query_count(
            self._command_tail(event)
        )
        if count_error:
            yield event.plain_result(count_error)
            return
        async for result in self._pixiv_results(
            event,
            "artist_random",
            "pixiv_user_random",
            user_id,
            return_count,
        ):
            yield result

    @filter.command("aaP???")
    async def pixiv_artist_find(self, event: AstrMessageEvent):
        async for result in self._pixiv_artist_find_command(event):
            yield result

    @filter.command("aaP?")
    async def pixiv_novel(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(
            event, "novel_search", "pixiv_novel", tags
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_novel_recommended(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "novel_recommended", "pixiv_novel_recommended"
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_novel_new(self, event: AstrMessageEvent, max_id: str = ""):
        async for result in self._pixiv_results(
            event, "novel_new", "pixiv_novel_new", max_id
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_novel_series(self, event: AstrMessageEvent, series_id: str = ""):
        async for result in self._pixiv_results(
            event, "novel_series", "pixiv_novel_series", series_id
        ):
            yield result

    @filter.command("aaP??")
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

    @filter.command("aaP??")
    async def pixiv_novel_download(self, event: AstrMessageEvent, novel_id: str = ""):
        async for result in self._pixiv_results(
            event, "novel_download", "pixiv_novel_download", novel_id
        ):
            yield result

    @filter.command("aaP?")
    async def pixiv_sub_add(self, event: AstrMessageEvent, artist_id: str = ""):
        async for result in self._pixiv_results(
            event, "subscriptions", "pixiv_subscribe_add", artist_id
        ):
            yield result

    @filter.command("aaP?")
    async def pixiv_sub_remove(self, event: AstrMessageEvent, artist_id: str = ""):
        async for result in self._pixiv_results(
            event, "subscriptions", "pixiv_subscribe_remove", artist_id
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_sub_list(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._pixiv_results(
            event, "subscriptions", "pixiv_subscribe_list", args
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_help(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._pixiv_results(event, "help", "pixiv_help", args):
            yield result

    @filter.command("aaP??")
    async def pixiv_random_add(self, event: AstrMessageEvent, tags: str = ""):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_add", tags
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_random_del(self, event: AstrMessageEvent, index: str = ""):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_del", index
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_random_list(self, event: AstrMessageEvent, args: str = ""):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_list", args
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_random_suspend(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_suspend"
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_random_resume(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_resume"
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_random_status(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_status"
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_random_force(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "random_search", "pixiv_random_force"
        ):
            yield result

    @filter.command("aaP???")
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

    @filter.command("aaP???")
    async def pixiv_random_ranking_del(
        self,
        event: AstrMessageEvent,
        index: str = "",
    ):
        async for result in self._pixiv_results(
            event, "random_ranking", "pixiv_random_ranking_del", index
        ):
            yield result

    @filter.command("aaP???")
    async def pixiv_random_ranking_list(
        self,
        event: AstrMessageEvent,
        args: str = "",
    ):
        async for result in self._pixiv_results(
            event, "random_ranking", "pixiv_random_ranking_list", args
        ):
            yield result

    @filter.command("aaP??")
    async def pixiv_trending(self, event: AstrMessageEvent):
        async for result in self._pixiv_results(
            event, "trending_tags", "pixiv_trending_tags"
        ):
            yield result

    @filter.command("aaPAI")
    async def pixiv_ai_setting(self, event: AstrMessageEvent, setting: str = ""):
        async for result in self._pixiv_results(
            event, "ai_display_setting", "pixiv_ai_show_settings", setting
        ):
            yield result

    @filter.command("aaP??")
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

    @filter.command("aaP?")
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

    @filter.command("aaF?")
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

    @filter.command("aaF?")
    async def fanbox_post(self, event: AstrMessageEvent, post: str = ""):
        async for result in self._pixiv_results(
            event, "fanbox_post", "pixiv_fanbox_post", post
        ):
            yield result

    @filter.command("aaF?")
    async def fanbox_recommended(self, event: AstrMessageEvent, limit: str = "5"):
        async for result in self._pixiv_results(
            event, "fanbox_recommended", "pixiv_fanbox_recommended", limit
        ):
            yield result

    @filter.command("aaF?")
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
