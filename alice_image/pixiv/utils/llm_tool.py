import base64
import hashlib
import io
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from fpdf import FPDF
from pydantic import Field
from pydantic.dataclasses import dataclass

from .pixiv_utils import (
    generate_safe_filename,
    send_forward_message,
    send_pixiv_image,
)
from .query_plan import (
    SORT_DATE_DESC,
    SORT_POPULAR_DESC,
    build_search_plan,
    dedupe_illusts,
    describe_api_error,
    detect_popular_desc_degraded,
    extract_autocomplete_tags,
    extract_illusts,
    extract_next_url,
    has_enough_results,
    options_from_config,
    sort_illusts_by_bookmarks,
)
from .tag import (
    FilterConfig,
    build_detail_message,
    filter_illusts_with_reason,
    process_and_send_illusts_sorted,
)


async def _resolve_normalized_tags(client, query: str, limit: int) -> list:
    """调用 search_autocomplete 把中文关键词归一化为 Pixiv 官方 tag。

    任何异常都吞掉并返回空列表，让调用方原样回退到旧的 partial 搜索行为。
    """
    import asyncio

    if not client or limit <= 0:
        return []
    for method_name in ("search_autocomplete_v2", "search_autocomplete"):
        method = getattr(client, method_name, None)
        if not callable(method):
            continue
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(method, query), timeout=10
            )
        except Exception as exc:
            logger.debug(f"autocomplete({method_name}) 失败，回退原关键词: {exc}")
            continue
        tags = extract_autocomplete_tags(result, limit)
        if tags:
            return tags
    return []


@dataclass
class PixivIllustSearchTool(FunctionTool[AstrAgentContext]):
    """
    Pixiv插画搜索工具
    """

    pixiv_client: Any = None
    pixiv_config: Any = None
    pixiv_client_wrapper: Any = None
    # 近期去重 / 选择策略（PixivSelectionPolicy），由 controller 注入
    selection_policy: Any = None
    name: str = "pixiv_search_illust"
    description: str = (
        "【图片/插画搜索专用工具】用于在Pixiv上搜索二次元插画、动漫图片、壁纸等。"
        "当用户想要：搜图、找图、来张图、发张图、看图、要壁纸、找插画、"
        "搜索某个角色/作品的图片（如'初音未来的图'、'原神壁纸'）时，必须使用此工具。"
        "此工具专门返回图片，不是网页搜索。任何涉及图片、插画、二次元图的请求都应优先使用本工具。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或标签，直接使用用户输入的原文。例如：初音ミク、原神、可爱女孩等",
                },
                "count": {
                    "type": "integer",
                    "description": (
                        "【必填】返回图片数量。"
                        "必须根据用户请求的数量填写！"
                        "例如：'来两张图'→count=2，'给我三张'→count=3，'来点图'→count=3。"
                        "如果用户没有明确说数量，默认设为1。最小1，最大5。"
                    ),
                    "minimum": 1,
                    "maximum": 5,
                    "default": 1,
                },
                "filters": {
                    "type": "string",
                    "description": "过滤条件：'safe'(全年龄)、'r18'(限制级)。默认为safe",
                },
            },
            "required": ["query"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        try:
            query = kwargs.get("query", "")
            count = min(max(int(kwargs.get("count", 1)), 1), 5)
            logger.info(f"Pixiv插画搜索工具：搜索 '{query}'，数量: {count}")

            if not self.pixiv_client:
                return "错误: Pixiv客户端未初始化"

            if (
                self.pixiv_client_wrapper
                and not await self.pixiv_client_wrapper.authenticate()
            ):
                if self.pixiv_config and hasattr(
                    self.pixiv_config, "get_auth_error_message"
                ):
                    return self.pixiv_config.get_auth_error_message()
                return "Pixiv API 认证失败，请检查配置中的凭据信息。"

            tags = query.strip()
            return await self._search_illust(tags, query, context, count)

        except Exception as e:
            logger.error(f"Pixiv插画搜索失败: {e}")
            return f"搜索失败: {e!s}"

    async def _search_illust(self, tags, query, context, count=1):
        """按热度（收藏数）搜索插画。

        与旧实现的差别：
        - 不再硬编码 duration="within_last_week"，一周外的经典作品也能被搜到
        - 默认使用 sort="popular_desc"，仅在检测到非会员被静默降级时回退本地排序
        - 走 query_plan 多级搜索计划（autocomplete 归一化 -> exact -> partial ->
          title_and_caption），autocomplete 失败时原样回退旧的 partial 行为
        """
        import asyncio

        options = options_from_config(self.pixiv_config)
        normalized_tags = []
        if options.enable_autocomplete:
            normalized_tags = await _resolve_normalized_tags(
                self.pixiv_client, tags, options.autocomplete_limit
            )
        plan = build_search_plan(
            tags,
            normalized_tags=normalized_tags,
            sort=SORT_POPULAR_DESC if options.prefer_popular else SORT_DATE_DESC,
            options=options,
        )
        if not plan:
            return f"未找到关于 '{query}' 的插画。"

        pages_per_step = 3
        # 至少要凑够 count 的若干倍候选，供过滤与去重消耗
        min_results = max(options.min_results, count * 4)
        all_illusts: list = []
        degraded = False
        last_error: str | None = None

        for step in plan:
            step_illusts: list = []
            next_params = None
            for page_index in range(pages_per_step):
                try:
                    if page_index == 0:
                        search_result = await asyncio.to_thread(
                            self.pixiv_client.search_illust,
                            **step.to_search_kwargs(),
                        )
                    else:
                        if not next_params:
                            break
                        search_result = await asyncio.to_thread(
                            self.pixiv_client.search_illust, **next_params
                        )
                except Exception as e:
                    logger.warning(
                        f"LLM 搜图（{step.reason}）第 {page_index + 1} 页出错: {e}"
                    )
                    break

                error_message = describe_api_error(search_result)
                if error_message:
                    last_error = error_message
                    logger.warning(f"LLM 搜图（{step.reason}）API 报错: {error_message}")
                    break

                page_illusts = extract_illusts(search_result)
                if not page_illusts:
                    break
                step_illusts.extend(page_illusts)

                next_url = extract_next_url(search_result)
                if not next_url:
                    break
                try:
                    next_params = self.pixiv_client.parse_qs(next_url)
                except Exception as e:
                    logger.debug(f"解析 next_url 失败: {e}")
                    break
                await asyncio.sleep(0.2)

            if step_illusts:
                if step.sort == SORT_POPULAR_DESC and detect_popular_desc_degraded(
                    step_illusts
                ):
                    degraded = True
                all_illusts.extend(step_illusts)
                all_illusts = dedupe_illusts(all_illusts)
                logger.info(
                    f"LLM 搜图（{step.reason}）累计候选 {len(all_illusts)} 个"
                )
                if has_enough_results(len(all_illusts), min_results):
                    break

        if not all_illusts:
            if last_error:
                return last_error
            return f"未找到关于 '{query}' 的插画。"

        if degraded:
            logger.info("检测到 popular_desc 被降级为时间序（非 Premium 账号），改用本地收藏数排序")
        # 无论是否降级都本地按收藏数排序：popular_desc 生效时该操作是幂等的
        sorted_illusts = sort_illusts_by_bookmarks(all_illusts)

        event = self._get_event(context)
        if event:
            return await self._send_pixiv_result(
                event, sorted_illusts, query, tags, count
            )
        return self._format_text_results(sorted_illusts, query, tags)

    async def _send_pixiv_result(self, event, items, query, tags, count=1):
        """发送按热度排序的结果"""
        logger.info(f"PixivIllustSearchTool: 准备发送 {count} 张图片")
        config = FilterConfig(
            r18_mode=self.pixiv_config.r18_mode if self.pixiv_config else "过滤 R18",
            filter_r18g_only=self.pixiv_config.filter_r18g_only
            if self.pixiv_config
            else False,
            ai_filter_mode=self.pixiv_config.ai_filter_mode
            if self.pixiv_config
            else "过滤 AI 作品",
            ai_detection_mode=self.pixiv_config.ai_detection_mode
            if self.pixiv_config
            else "field_or_tag",
            display_tag_str=f"搜索:{query}",
            return_count=count,
            logger=logger,
            show_filter_result=False,
            single_response_mode=self.pixiv_config.single_response_mode
            if self.pixiv_config
            else False,
            excluded_tags=[],
            forward_threshold=self.pixiv_config.forward_threshold
            if self.pixiv_config
            else False,
            show_details=self.pixiv_config.show_details if self.pixiv_config else True,
        )

        filtered_items, _ = filter_illusts_with_reason(items, config)
        if not filtered_items:
            return "找到插画但被过滤了 (可能是R18或AI作品)。"

        if not hasattr(event, "send"):
            return self._format_text_results(filtered_items, query, tags)

        expected_count = min(len(filtered_items), config.return_count)
        sent_batches = 0

        # 排序链路必须禁用随机采样，否则收藏数排序会被 random.sample 作废；
        # 同时注入选择策略以启用「近期已发送去重」。
        selection_func = None
        if self.selection_policy is not None:
            try:
                selection_func = self.selection_policy.callback(event, randomize=False)
            except Exception as e:
                logger.warning(f"构造选择策略回调失败，退化为顺序取图: {e}")

        try:
            async for result in process_and_send_illusts_sorted(
                filtered_items,
                config,
                self.pixiv_client,
                event,
                build_detail_message,
                send_pixiv_image,
                send_forward_message,
                is_novel=False,
                selection_func=selection_func,
            ):
                try:
                    await event.send(result)
                    sent_batches += 1
                except Exception as e:
                    logger.warning(f"发送图片失败: {e}")

            if sent_batches > 0:
                mode = "转发消息" if config.forward_threshold else "普通消息"
                return (
                    f"🔥 找到了！为您发送了「{query}」最热门的"
                    f" {expected_count} 张作品（{mode}）。"
                )

            return "找到插画但发送失败，请稍后再试。"
        except Exception as e:
            logger.error(f"发送失败: {e}")
            return "找到插画但发送过程中出现异常。"

    def _get_event(self, context):
        try:
            agent_context = context.context if hasattr(context, "context") else context
            if hasattr(context, "event") and context.event:
                return context.event
            if hasattr(agent_context, "event") and agent_context.event:
                return agent_context.event
        except Exception:
            pass
        return None

    def _format_text_results(self, items, query, tags):
        result = "找到以下插画:\n"
        for i, item in enumerate(items[:5], 1):
            title = getattr(item, "title", "未知标题")
            result += f"{i}. {title} (ID: {item.id})\n"
        return result


@dataclass
class PixivNovelSearchTool(FunctionTool[AstrAgentContext]):
    """
    Pixiv小说搜索工具
    """

    pixiv_client: Any = None
    pixiv_config: Any = None
    pixiv_client_wrapper: Any = None

    name: str = "pixiv_search_novel"
    description: str = "Pixiv小说搜索工具。用于搜索Pixiv上的小说，或者通过ID直接下载小说。支持输入关键词或纯数字ID。"
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或小说ID（纯数字）。",
                },
                "filters": {
                    "type": "string",
                    "description": "过滤条件，如 'safe', 'r18' 等",
                },
            },
            "required": ["query"],
        }
    )

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        try:
            query = kwargs.get("query", "")
            logger.info(f"Pixiv小说搜索工具：搜索 '{query}'")

            if not self.pixiv_client:
                return "错误: Pixiv客户端未初始化"

            if (
                self.pixiv_client_wrapper
                and not await self.pixiv_client_wrapper.authenticate()
            ):
                if self.pixiv_config and hasattr(
                    self.pixiv_config, "get_auth_error_message"
                ):
                    return self.pixiv_config.get_auth_error_message()
                return "Pixiv API 认证失败，请检查配置中的凭据信息。"

            tags = query.strip()
            return await self._search_novel(tags, query, context)

        except Exception as e:
            logger.error(f"Pixiv小说搜索失败: {e}")
            return f"搜索失败: {e!s}"

    async def _search_novel(self, tags, query, context):
        import asyncio

        # ID 检查
        if query.isdigit():
            logger.info(f"检测到小说ID {query}")
            try:
                novel_detail = await asyncio.to_thread(
                    self.pixiv_client.novel_detail, int(query)
                )
                if novel_detail and novel_detail.novel:
                    event = self._get_event(context)
                    if event:
                        return await self._send_novel_result(
                            event, [novel_detail.novel], query, tags
                        )
                    return f"找到小说: {novel_detail.novel.title} (ID: {query})，但无法发送文件(无事件上下文)。"
                return f"未找到ID为 {query} 的小说。"
            except Exception as e:
                return f"获取小说详情失败: {e!s}"

        # 标签搜索：先精确匹配 tag，再回退部分匹配（与旧行为一致的兜底）
        try:
            search_result = None
            novels = []
            for search_target in ("exact_match_for_tags", "partial_match_for_tags"):
                try:
                    search_result = await asyncio.to_thread(
                        self.pixiv_client.search_novel,
                        tags,
                        search_target=search_target,
                    )
                except Exception as e:
                    logger.warning(f"小说搜索（{search_target}）失败: {e}")
                    continue
                novels = list(getattr(search_result, "novels", None) or [])
                if novels:
                    break

            if novels:
                event = self._get_event(context)
                if event:
                    return await self._send_novel_result(event, novels, query, tags)
                return self._format_text_results(novels, query, tags)
            return f"未找到关于 '{query}' 的小说。"
        except Exception as e:
            return f"API调用错误: {e!s}"

    async def _send_novel_result(self, event, items, query, tags):
        import asyncio

        if not items:
            return "未找到小说。"

        selected_item = items[0]  # 取第一个
        novel_id = str(selected_item.id)
        novel_title = selected_item.title

        logger.info(f"准备下载小说 {novel_title} (ID: {novel_id})")

        try:
            novel_content_result = await asyncio.to_thread(
                self.pixiv_client.webview_novel, novel_id
            )
            if not novel_content_result or not hasattr(novel_content_result, "text"):
                return f"无法获取小说内容 (ID: {novel_id})。"

            novel_text = novel_content_result.text

            try:
                pdf_bytes = await asyncio.to_thread(
                    self._create_pdf_from_text, novel_title, novel_text
                )
            except FileNotFoundError:
                return "无法生成PDF：字体文件丢失。"
            except Exception as e:
                return f"生成PDF失败: {e!s}"

            # 加密
            password = hashlib.md5(novel_id.encode()).hexdigest()
            final_pdf_bytes = pdf_bytes
            password_notice = ""
            try:
                from PyPDF2 import PdfReader, PdfWriter

                reader = PdfReader(io.BytesIO(pdf_bytes))
                writer = PdfWriter()
                for page in reader.pages:
                    writer.add_page(page)
                writer.encrypt(password)
                with io.BytesIO() as bs:
                    writer.write(bs)
                    final_pdf_bytes = bs.getvalue()
                password_notice = f"PDF已加密，密码: {password}"
            except Exception:
                password_notice = "PDF未加密。"

            # 发送
            safe_title = generate_safe_filename(novel_title, "novel")
            file_name = f"{safe_title}_{novel_id}.pdf"

            file_sent = False
            if event.get_platform_name() == "aiocqhttp" and event.get_group_id():
                try:
                    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                        AiocqhttpMessageEvent,
                    )

                    if isinstance(event, AiocqhttpMessageEvent):
                        client_bot = event.bot
                        group_id = event.get_group_id()
                        file_base64 = base64.b64encode(final_pdf_bytes).decode("utf-8")
                        await client_bot.upload_group_file(
                            group_id=group_id,
                            file=f"base64://{file_base64}",
                            name=file_name,
                        )
                        file_sent = True
                except Exception as e:
                    logger.error(f"群文件上传失败: {e}")

            author = (
                getattr(selected_item.user, "name", "未知作者")
                if hasattr(selected_item, "user")
                else "未知作者"
            )

            if file_sent:
                return f"已下载小说：\n**{novel_title}** - {author}\nID: {novel_id}\n文件已上传到群文件。\n{password_notice}\n(任务完成)"
            return f"已找到小说：\n**{novel_title}** - {author}\nID: {novel_id}\n无法发送文件，请尝试手动下载。\n(任务完成)"

        except Exception as e:
            logger.error(f"处理小说失败: {e}")
            return f"处理小说失败: {e!s}"

    def _create_pdf_from_text(self, title: str, text: str) -> bytes:
        font_path = Path(__file__).parent.parent / "data" / "SmileySans-Oblique.ttf"
        if not font_path.exists():
            raise FileNotFoundError(f"字体文件不存在: {font_path}")

        pdf = FPDF()
        pdf.add_page()
        pdf.add_font("SmileySans", "", str(font_path), uni=True)
        pdf.set_font("SmileySans", size=20)
        pdf.multi_cell(0, 10, title, align="C")
        pdf.ln(10)
        pdf.set_font_size(12)
        pdf.multi_cell(0, 10, text)
        return pdf.output(dest="S")

    def _get_event(self, context):
        try:
            agent_context = context.context if hasattr(context, "context") else context
            if hasattr(context, "event") and context.event:
                return context.event
            if hasattr(agent_context, "event") and agent_context.event:
                return agent_context.event
        except Exception:
            pass
        return None

    def _format_text_results(self, items, query, tags):
        result = "找到以下小说:\n"
        for i, item in enumerate(items[:5], 1):
            title = getattr(item, "title", "未知标题")
            result += f"{i}. {title} (ID: {item.id})\n"
        return result


def create_pixiv_llm_tools(
    pixiv_client=None,
    pixiv_config=None,
    pixiv_client_wrapper=None,
    selection_policy=None,
) -> list[FunctionTool]:
    """
    创建Pixiv相关的LLM工具列表
    """
    logger.info(
        "创建Pixiv LLM工具，pixiv_client: %s, wrapper: %s",
        "已设置" if pixiv_client else "未设置",
        "已设置" if pixiv_client_wrapper else "未设置",
    )

    tools = [
        PixivIllustSearchTool(
            pixiv_client=pixiv_client,
            pixiv_config=pixiv_config,
            pixiv_client_wrapper=pixiv_client_wrapper,
            selection_policy=selection_policy,
        ),
        PixivNovelSearchTool(
            pixiv_client=pixiv_client,
            pixiv_config=pixiv_config,
            pixiv_client_wrapper=pixiv_client_wrapper,
        ),
    ]
    logger.info(f"已创建 {len(tools)} 个LLM工具")
    return tools
