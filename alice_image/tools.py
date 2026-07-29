"""唯一命名空间下的 LLM 工具定义。"""

from __future__ import annotations

from typing import Any

from pydantic import Field
from pydantic.dataclasses import dataclass

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext


def _event_from_context(context: ContextWrapper[AstrAgentContext]):
    return getattr(getattr(context, "context", None), "event", None)


@dataclass
class AliceFindImageTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "alice_image_find"
    description: str = (
        "为用户精确搜索并发送图片。可在 Pixiv、通用网页图源和 SerpApi 之间自由选源；"
        "source=auto 时由助手判断，失败时插件会按配置自动回退。"
        "需要指定 Pixiv 作者/画师时，填写 artist_name 或 pixiv_user_id。"
        "visual_description 只写画面中确实需要匹配的主体、外观和风格，不要捏造罕见动作或场景。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索引擎关键词。Pixiv 请求优先使用准确的日文或英文标签。",
                },
                "visual_description": {
                    "type": "string",
                    "description": "用于视觉审核挑图的准确画面描述；不需要审核时可与 query 相同。",
                },
                "source": {
                    "type": "string",
                    "enum": ["auto", "pixiv", "soutu", "serpapi"],
                    "description": "找图来源。auto 自动选择；也可明确指定。",
                },
                "artist_name": {
                    "type": "string",
                    "description": "可选。指定 Pixiv 画师名或账号；填写后会限定在该画师作品中找图。",
                },
                "pixiv_user_id": {
                    "type": "string",
                    "description": "可选。Pixiv 用户 ID，比 artist_name 更精确；填写后会限定在该用户作品中找图。",
                },
                "count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "需要的图片数量；通用来源当前每次发送一张，Pixiv 最多五张。",
                },
                "is_explanation": {
                    "type": "boolean",
                    "description": "主动为实体介绍配图时为 true；用户明确要求找图时为 false。",
                },
            },
            "required": ["query", "visual_description"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return '{"success": false, "error": "工具缺少事件上下文"}'
        return await self.plugin.tool_find_image(
            event=event,
            query=kwargs.get("query", ""),
            description=kwargs.get("visual_description", ""),
            source=kwargs.get("source", "auto"),
            count=kwargs.get("count", 1),
            is_explanation=bool(kwargs.get("is_explanation", False)),
            artist_name=kwargs.get("artist_name", ""),
            pixiv_user_id=kwargs.get("pixiv_user_id", ""),
        )


@dataclass
class AliceReverseImageTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "alice_image_reverse_search"
    description: str = (
        "查找用户已发送图片的来源。若 alice_image_list_session_images 可用，先查看图片并"
        "优先用稳定的 image_id 选择目标；否则可直接搜索最新图片或使用 image_index。"
        "支持 SauceNAO、Google Lens 和 Ascii2d。"
    )
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "image_id": {
                    "type": "string",
                    "description": "会话图片列表返回的稳定 image_id。",
                },
                "image_index": {
                    "type": "integer",
                    "description": "兼容索引：-1 为最新，1 为最早。image_id 优先。",
                },
                "strategies": {
                    "type": "string",
                    "description": "可选，逗号分隔：saucenao,google,ascii2d。留空使用全部。",
                },
            },
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return '{"success": false, "error": "工具缺少事件上下文"}'
        return await self.plugin.tool_reverse_image(
            event=event,
            image_id=kwargs.get("image_id"),
            image_index=kwargs.get("image_index", -1),
            strategies=kwargs.get("strategies"),
        )


@dataclass
class AliceSessionImagesTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "alice_image_list_session_images"
    description: str = "列出当前会话中可供以图搜图选择的图片及其 image_id。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return '{"success": false, "error": "工具缺少事件上下文"}'
        return await self.plugin.tool_list_session_images(event)


@dataclass
class AlicePixivNovelTool(FunctionTool[AstrAgentContext]):
    plugin: Any = Field(default=None, repr=False)
    name: str = "alice_image_pixiv_novel"
    description: str = "搜索 Pixiv 小说，或按纯数字小说 ID 获取并下载小说。"
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Pixiv 小说标签、关键词或纯数字小说 ID。",
                }
            },
            "required": ["query"],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> str:
        event = _event_from_context(context)
        if self.plugin is None or event is None:
            return '{"success": false, "error": "工具缺少事件上下文"}'
        return await self.plugin.tool_pixiv_novel(event, kwargs.get("query", ""))
