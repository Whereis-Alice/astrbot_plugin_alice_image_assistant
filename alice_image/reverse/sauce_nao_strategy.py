"""SauceNAO 搜图策略实现.

使用 SauceNAO API 进行图片搜索。
"""

from __future__ import annotations

import json
from typing import Any

import aiohttp
from astrbot.api import logger

from .constant import (
    DEFAULT_SAUCENAO_NUMRES,
    DEFAULT_SAUCENAO_SIMILARITY_THRESHOLD,
    HTTP_TIMEOUT_SECONDS,
    SAUCENAO_BASE_URL,
    SOURCE_KEY_SAUCENAO,
)
from .models import SearchResultItem
from .strategy import ImageSearchStrategy
from .utils import coerce_int, get_aiohttp_session, get_proxy_url, get_user_agent


class SauceNaoStrategy(ImageSearchStrategy):
    """SauceNAO 搜图策略.

    SauceNAO 是一个强大的动漫图片搜索引擎，支持多个数据库。
    """

    def __init__(
        self,
        api_key: str | None = None,
        similarity_threshold: int = DEFAULT_SAUCENAO_SIMILARITY_THRESHOLD,
        max_results: int = DEFAULT_SAUCENAO_NUMRES,
    ) -> None:
        """初始化 SauceNAO 策略.

        Args:
            api_key: SauceNAO API Key，可选但推荐使用以获得更高配额
            similarity_threshold: 相似度阈值 (0-100)，低于此值的结果将被过滤
            max_results: 单次请求返回条数 (numres)
        """
        self.api_key = api_key
        # WebUI 保存的数值配置可能是字符串，直接 max/min 会抛 TypeError 导致插件初始化失败，
        # 因此统一先强制转换再夹取范围。
        self.similarity_threshold = coerce_int(
            similarity_threshold, DEFAULT_SAUCENAO_SIMILARITY_THRESHOLD, 0, 100
        )
        self.max_results = coerce_int(max_results, DEFAULT_SAUCENAO_NUMRES, 1, 30)

    def get_service_name(self) -> str:
        return "SauceNAO"

    async def search(self, image_url: str) -> list[SearchResultItem]:
        """执行 SauceNAO 图片搜索.

        Args:
            image_url: 图片 URL 地址

        Returns:
            搜索结果列表
        """
        if not self.api_key:
            logger.warning("[SauceNAO] 未配置 API Key，跳过搜索")
            return []

        if not image_url.startswith(("http://", "https://")):
            logger.warning(f"[SauceNAO] 不支持的图片格式: {image_url}")
            return []

        results: list[SearchResultItem] = []

        try:
            session = await get_aiohttp_session()

            # 使用 URL 参数构建请求
            params = {
                "api_key": self.api_key,
                "output_type": "2",  # JSON 输出
                "numres": str(self.max_results),  # 返回结果数量 (可配置)
                "url": image_url,
            }

            headers = {
                "User-Agent": get_user_agent(),
            }

            timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
            proxy = get_proxy_url()
            async with session.get(
                SAUCENAO_BASE_URL,
                params=params,
                headers=headers,
                timeout=timeout,
                proxy=proxy,
            ) as resp:
                if resp.status != 200:
                    logger.error(f"[SauceNAO] API 请求失败: HTTP {resp.status}")
                    return results

                text = await resp.text()
                json_data = json.loads(text)

                # 检查是否有错误
                if "results" not in json_data:
                    # 检查是否是错误响应
                    if "header" in json_data:
                        header = json_data["header"]
                        status = header.get("status", 0)
                        if status != 0:
                            message = header.get("message", "未知错误")
                            logger.error(f"[SauceNAO] API 错误: {message}")
                    return results

                nodes = json_data["results"]
                if not isinstance(nodes, list):
                    logger.error("[SauceNAO] API 返回的 results 不是列表，已忽略")
                    return results

                for node in nodes:
                    # 每条结果单独 try：单条脏数据 (similarity 为 None/dict 等) 只跳过它自己，
                    # 而不是让异常逃到外层 except 把后续结果全部静默截断。
                    try:
                        item = self._parse_node(node)
                    except (TypeError, ValueError) as e:
                        logger.warning(f"[SauceNAO] 跳过无法解析的结果项: {e}")
                        continue
                    if item is not None:
                        results.append(item)

        except Exception as e:
            logger.error(f"[SauceNAO] 搜索失败: {e}")

        return results

    def _parse_node(self, node: Any) -> SearchResultItem | None:
        """解析单条 SauceNAO 结果.

        Args:
            node: SauceNAO results 数组中的一项

        Returns:
            结果项；相似度低于阈值或无可用链接时返回 None

        Raises:
            TypeError: 结果项结构异常 (由调用方逐条捕获)
            ValueError: 相似度无法解析成数值 (由调用方逐条捕获)
        """
        if not isinstance(node, dict):
            raise TypeError(f"结果项不是字典: {type(node).__name__}")

        header = node.get("header")
        header = header if isinstance(header, dict) else {}
        data_section = node.get("data")
        data_section = data_section if isinstance(data_section, dict) else {}

        # similarity 可能是 None/dict，float() 会抛 TypeError，交由调用方逐条捕获
        similarity = float(header.get("similarity", 0) or 0)

        # 相似度阈值过滤：阈值越低结果越多，但误命中也越多
        if similarity < self.similarity_threshold:
            return None

        title = self._extract_title(data_section)
        thumbnail = header.get("thumbnail")
        thumbnail = thumbnail if isinstance(thumbnail, str) else ""

        # ext_urls 可能不是列表 (脏数据)，也可能为空；为空时生成 url="" 会展示成空链接，
        # 因此退回缩略图链接，连缩略图都没有就直接跳过该条。
        ext_urls = data_section.get("ext_urls")
        ext_url = ""
        if isinstance(ext_urls, list):
            for candidate in ext_urls:
                if isinstance(candidate, str) and candidate.strip():
                    ext_url = candidate.strip()
                    break
        if not ext_url:
            if thumbnail.startswith(("http://", "https://")):
                ext_url = thumbnail
            else:
                logger.debug(f"[SauceNAO] 结果 '{title}' 无可用外链，已跳过")
                return None

        return SearchResultItem(
            title=title,
            url=ext_url,
            thumbnail=thumbnail,
            thumbnail_bytes=None,
            source="SauceNAO",
            # similarity 继续输出人类可读字符串，保持既有展示逻辑向后兼容
            similarity=f"{similarity:.2f}%",
            description=None,
            domain=None,
            # score 是跨引擎统一的归一化置信度，SauceNAO 直接用相似度百分比换算
            score=max(0.0, min(1.0, similarity / 100.0)),
            source_key=SOURCE_KEY_SAUCENAO,
        )

    @staticmethod
    def _extract_title(data: dict[str, Any]) -> str:
        """从 SauceNAO 数据中提取标题.

        Args:
            data: SauceNAO 结果的 data 部分

        Returns:
            提取的标题字符串
        """
        # 按优先级尝试不同的标题字段 (只接受字符串，避免脏数据污染展示)
        for key in ("title", "eng_name", "jp_name", "material", "source"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        # Pixiv 作者名
        member_name = data.get("member_name")
        if isinstance(member_name, str) and member_name.strip():
            return f"Artist: {member_name.strip()}"

        return "SauceNAO Result"
