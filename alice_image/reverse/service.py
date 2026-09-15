"""图片搜索服务.

协调各搜图策略，并行执行搜索、跨引擎融合排序并下载缩略图。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from astrbot.api import logger

from .constant import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_TOTAL_TIMEOUT_SECONDS,
    MAX_TOTAL_TIMEOUT_SECONDS,
    MIN_PER_ENGINE_FETCH,
    MIN_TOTAL_TIMEOUT_SECONDS,
    STRATEGY_ALIAS_MAP,
)
from .models import ExplorationResult, SearchResultItem
from .ranking import merge_and_rank
from .routing import IntentRoute, route_intent
from .strategy import ImageSearchStrategy
from .utils import coerce_int, download_bytes


class AliceImageReverseService:
    """图片搜索服务.

    负责协调多个搜图策略并行执行，融合排序结果并下载缩略图。
    """

    def __init__(
        self,
        strategies: list[ImageSearchStrategy],
        max_results: int = DEFAULT_MAX_RESULTS,
        total_timeout_seconds: int = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        """初始化搜索服务.

        Args:
            strategies: 搜图策略列表
            max_results: 最终展示条数上限 (跨引擎融合去重后的总条数)
            total_timeout_seconds: 单个策略的总超时时间 (秒)
        """
        self.strategies = strategies
        # display.max_results 的语义是"最终展示条数"，不是"每引擎条数"
        self.max_results = coerce_int(max_results, DEFAULT_MAX_RESULTS, 1, 10)
        # 每引擎抓取上限可以大于展示上限：跨引擎去重会吃掉一部分结果，
        # 抓少了会导致融合后凑不满展示条数。
        self.per_engine_fetch = max(self.max_results, MIN_PER_ENGINE_FETCH)
        self.total_timeout_seconds = coerce_int(
            total_timeout_seconds,
            DEFAULT_TOTAL_TIMEOUT_SECONDS,
            MIN_TOTAL_TIMEOUT_SECONDS,
            MAX_TOTAL_TIMEOUT_SECONDS,
        )
        # 建立策略名称索引
        self._strategy_map: dict[str, ImageSearchStrategy] = {}
        for strategy in self.strategies:
            name = strategy.get_service_name().lower()
            self._strategy_map[name] = strategy

    def get_available_strategies(self) -> list[str]:
        """获取当前可用的策略名称列表.

        Returns:
            策略名称列表
        """
        return [s.get_service_name() for s in self.strategies]

    def resolve_intent(self, intent: str | None) -> IntentRoute:
        """根据自然语言意图选择当前已加载的一个反查策略。

        为空或无法识别时返回空路由，调用方应继续使用原有的全策略并行行为。
        """

        return route_intent(intent, self.get_available_strategies())

    def resolve_strategy_names(
        self, names: list[str] | None
    ) -> tuple[list[ImageSearchStrategy], list[str]]:
        """解析策略名称别名，返回对应的策略实例.

        Args:
            names: 策略名称或别名列表，None 或空列表表示使用全部策略

        Returns:
            (匹配的策略实例列表, 未找到的策略名称列表)
        """
        if not names:
            return self.strategies, []

        resolved: list[ImageSearchStrategy] = []
        not_found: list[str] = []

        for name in names:
            name_lower = name.lower().strip()
            # 先通过别名映射
            canonical_name = STRATEGY_ALIAS_MAP.get(name_lower, name_lower)
            # 查找策略
            strategy = self._strategy_map.get(canonical_name.lower())
            if strategy:
                # 已找到策略，避免重复添加
                if strategy not in resolved:
                    resolved.append(strategy)
                # 如果是重复输入，则忽略，不计入 not_found
            else:
                # 未找到对应策略，记录到 not_found 并打印日志
                not_found.append(name)
                logger.warning(f"[AliceImageReverse] 未找到策略 '{name}'")

        return resolved, not_found

    async def _run_strategy(
        self, strategy: ImageSearchStrategy, image_url: str
    ) -> list[SearchResultItem]:
        """执行单个策略并施加总超时.

        超时只掐掉当前策略，其它策略的结果照常保留，避免一个慢引擎
        (ascii2d 内部有多次串行请求) 把整次搜图拖成空结果。

        Args:
            strategy: 搜图策略
            image_url: 图片 URL

        Returns:
            该策略的结果列表，超时或异常时返回空列表
        """
        name = strategy.get_service_name()
        try:
            items = await asyncio.wait_for(
                strategy.search(image_url), timeout=self.total_timeout_seconds
            )
        except TimeoutError:
            logger.warning(
                f"[AliceImageReverse] 策略 [{name}] 超过 "
                f"{self.total_timeout_seconds}s 总超时，已放弃该引擎结果"
            )
            return []
        except Exception as e:
            logger.error(f"[AliceImageReverse] 策略 [{name}] 执行失败: {e}")
            return []

        if not isinstance(items, list):
            logger.warning(f"[AliceImageReverse] 策略 [{name}] 返回了非列表结果，已忽略")
            return []
        return items[: self.per_engine_fetch]

    async def explore(
        self, image_url: str, strategy_names: list[str] | None = None
    ) -> ExplorationResult:
        """执行图片搜索.

        Args:
            image_url: 图片 URL 地址
            strategy_names: 指定使用的策略名称列表，None 表示使用所有策略

        Returns:
            包含融合排序后结果的 ExplorationResult
        """
        # 解析要使用的策略
        strategies_to_use, not_found = self.resolve_strategy_names(strategy_names)

        # 如果指定了策略但全部未找到，返回空结果并记录错误
        if strategy_names and not strategies_to_use:
            available = self.get_available_strategies()
            logger.warning(
                f"[AliceImageReverse] 指定的策略 {strategy_names} 全部不可用，"
                f"当前可用策略: {available}"
            )
            return ExplorationResult()

        if not strategies_to_use:
            logger.warning("[AliceImageReverse] 未找到任何可用的搜图策略")
            return ExplorationResult()

        start_time = time.monotonic()
        strategy_names_str = ", ".join(s.get_service_name() for s in strategies_to_use)
        if not_found:
            logger.warning(f"[AliceImageReverse] 以下策略未找到: {not_found}")
        logger.info(
            f"[AliceImageReverse] 开始搜图，目标 URL: {image_url}, 使用策略: {strategy_names_str}"
        )

        try:
            # 并行调用所有策略；每个策略单独包超时与异常，互不牵连
            results_list = await asyncio.gather(
                *[
                    self._run_strategy(strategy, image_url)
                    for strategy in strategies_to_use
                ],
                return_exceptions=True,
            )

            # 聚合结果
            all_items: list[SearchResultItem] = []
            for index, result in enumerate(results_list):
                if isinstance(result, BaseException):
                    logger.error(
                        f"[AliceImageReverse] 策略 "
                        f"[{strategies_to_use[index].get_service_name()}] 异常: {result}"
                    )
                    continue
                all_items.extend(result)

            # 先跨引擎融合排序去重，再按展示上限截断，保证高置信度结果不被挤掉
            items = merge_and_rank(all_items, self.max_results)
            logger.info(
                f"[AliceImageReverse] 搜索完成，原始 {len(all_items)} 条 -> "
                f"融合后 {len(items)} 条，开始下载缩略图..."
            )

            # 并行下载缩略图 (只下载最终要展示的那几条)
            await self._fill_thumbnails(items)

            elapsed = time.monotonic() - start_time
            logger.info(f"[AliceImageReverse] 任务结束，总耗时: {elapsed:.2f}s")

            return ExplorationResult(items=items)

        except Exception as e:
            logger.error(f"[AliceImageReverse] 搜索主流程异常: {e}")
            return ExplorationResult()

    def _get_thumbnail_fetcher(
        self, item: SearchResultItem
    ) -> Callable[[str], Awaitable[bytes | None]]:
        """获取结果项对应的缩略图下载函数.

        ascii2d 等站点需要用策略自己的会话 (指纹 + Cookie + Referer) 才能取图，
        因此优先使用策略提供的 fetch_thumbnail 钩子。

        Args:
            item: 搜索结果项

        Returns:
            缩略图下载协程函数
        """
        strategy = self._strategy_map.get((item.source or "").strip().lower())
        fetcher = getattr(strategy, "fetch_thumbnail", None)
        if callable(fetcher):
            return fetcher
        return download_bytes

    async def _fill_thumbnails(self, items: list[SearchResultItem]) -> None:
        """并行下载缩略图并回填到结果项中.

        Args:
            items: 搜索结果列表 (原地更新)
        """
        # 找出需要下载缩略图的项
        targets = [
            index
            for index, item in enumerate(items)
            if item.thumbnail_bytes is None and item.thumbnail
        ]
        if not targets:
            return

        # return_exceptions=True：一张缩略图失败 (或被取消) 不该让整批结果丢失，
        # 缩略图只是附加信息，降级为"无图"即可。
        results = await asyncio.gather(
            *[
                self._get_thumbnail_fetcher(items[index])(items[index].thumbnail)
                for index in targets
            ],
            return_exceptions=True,
        )

        for index, bytes_data in zip(targets, results, strict=True):
            if isinstance(bytes_data, BaseException):
                logger.debug(
                    f"[AliceImageReverse] 缩略图下载失败，降级为无图: {bytes_data}"
                )
                continue
            if bytes_data:
                items[index] = items[index].with_thumbnail_bytes(bytes_data)
