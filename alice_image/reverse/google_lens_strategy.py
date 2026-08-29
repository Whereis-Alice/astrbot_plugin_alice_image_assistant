"""Google Lens 搜图策略实现.

通过 SerpAPI 调用 Google Lens 进行图片搜索。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse

import aiohttp
from astrbot.api import logger

from .constant import (
    DEFAULT_GOOGLE_LENS_MAX_RESULTS,
    HTTP_TIMEOUT_SECONDS,
    SERPAPI_BASE_URL,
    SERPAPI_KEY_PENALTY_SECONDS,
    SOURCE_KEY_GOOGLE_LENS,
)
from .models import SearchResultItem
from .ranking import positional_score
from .strategy import ImageSearchStrategy
from .utils import coerce_int, get_aiohttp_session, get_proxy_url

# 额度缓存 TTL（秒）
QUOTA_CACHE_TTL = 60


class SerpApiQuotaExhaustedError(RuntimeError):
    """SerpAPI Key 额度耗尽异常."""

    def __init__(self, api_key: str, status: int | None = None) -> None:
        self.api_key = api_key
        self.status = status
        super().__init__(f"SerpAPI key exhausted: ...{api_key[-4:]} (status={status})")


class GoogleLensStrategy(ImageSearchStrategy):
    """Google Lens 搜图策略.

    使用 SerpAPI 的 google_lens 引擎进行图片搜索。
    支持多 API Key 负载均衡和余额检查。
    """

    def __init__(
        self,
        api_keys: list[str] | None = None,
        max_results: int = DEFAULT_GOOGLE_LENS_MAX_RESULTS,
    ) -> None:
        """初始化 Google Lens 策略.

        Args:
            api_keys: SerpAPI API Key 列表，支持多 Key 负载均衡
            max_results: 单次取用的 visual_matches 条数
        """
        self.api_keys = api_keys or []
        # WebUI 传来的数值可能是字符串，先强制转换再夹取，避免初始化期崩溃
        self.max_results = coerce_int(max_results, DEFAULT_GOOGLE_LENS_MAX_RESULTS, 1, 30)
        self._current_key_index = 0
        self._key_lock = asyncio.Lock()
        # 额度缓存: {api_key: (searches_left, timestamp)}
        self._quota_cache: dict[str, tuple[int, float]] = {}
        # 失败 Key 的短时惩罚: {api_key: 冷却截止时间戳}
        # 只惩罚不拉黑：网络抖动导致的失败不应该让 Key 永久不可用
        self._key_penalty_until: dict[str, float] = {}

    def get_service_name(self) -> str:
        return "Google Lens"

    async def search(self, image_url: str) -> list[SearchResultItem]:
        """执行 Google Lens 搜索.

        Args:
            image_url: 图片 URL 地址

        Returns:
            搜索结果列表
        """
        if not self.api_keys:
            logger.warning("[GoogleLens] 未配置 SerpAPI Key，跳过搜索")
            return []

        if not image_url.startswith(("http://", "https://")):
            logger.warning("[GoogleLens] SerpAPI 不支持本地文件")
            return []

        # 尝试所有可用的 API Key
        last_exception: Exception | None = None
        for attempt in range(len(self.api_keys)):
            try:
                return await self._search_with_key(image_url)
            except SerpApiQuotaExhaustedError as e:
                logger.warning(
                    f"[GoogleLens] Key ...{e.api_key[-4:]} 额度已耗尽，"
                    f"尝试使用下一个可用 Key (尝试 {attempt + 1}/{len(self.api_keys)})"
                )
                # 继续尝试下一个 key
                continue
            except Exception as e:
                # 记录异常但继续尝试其他 key
                logger.warning(
                    f"[GoogleLens] Key 尝试失败 (尝试 {attempt + 1}/{len(self.api_keys)}): {e}"
                )
                last_exception = e
                continue

        # 所有 Key 都失败
        if last_exception:
            logger.error(
                f"[GoogleLens] 所有 API Key 均失败，最后错误: {last_exception}"
            )
        else:
            logger.error("[GoogleLens] 所有 API Key 已耗尽")
        return []

    async def _search_with_key(self, image_url: str) -> list[SearchResultItem]:
        """使用当前选中的 Key 执行搜索.

        Args:
            image_url: 图片 URL 地址

        Returns:
            搜索结果列表

        Raises:
            SerpApiQuotaExhaustedError: 当 API Key 额度耗尽时抛出
        """
        # 选择可用的 API Key（乐观选择，不预先检查额度）
        api_key = await self._select_key_optimistically()
        if not api_key:
            raise SerpApiQuotaExhaustedError("", status=None)

        logger.info(f"[GoogleLens] 使用 Key ...{api_key[-4:]} 开始搜索")

        try:
            return await self._request_with_key(api_key, image_url)
        except SerpApiQuotaExhaustedError:
            # 额度耗尽已经通过 _quota_cache 记录，无需重复惩罚
            raise
        except Exception:
            # 非额度错误（超时、解析失败等）也要给该 Key 一段冷却，
            # 否则外层重试会立刻再次挑到同一个坏 Key。
            await self._penalize_key(api_key)
            raise

    async def _request_with_key(
        self, api_key: str, image_url: str
    ) -> list[SearchResultItem]:
        """用指定 Key 请求 SerpAPI 并解析结果.

        Args:
            api_key: 使用的 SerpAPI Key
            image_url: 图片 URL 地址

        Returns:
            搜索结果列表

        Raises:
            SerpApiQuotaExhaustedError: 当 API Key 额度耗尽时抛出
        """

        # 构建 SerpAPI 请求
        params = {
            "api_key": api_key,
            "engine": "google_lens",
            "url": image_url,
            "hl": "zh-cn",
        }

        url = f"{SERPAPI_BASE_URL}/search?{urllib.parse.urlencode(params)}"

        session = await get_aiohttp_session()
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        proxy = get_proxy_url()

        async with session.get(url, timeout=timeout, proxy=proxy) as resp:
            if resp.status != 200:
                # 处理额度耗尽错误：标记当前 key 耗尽，并抛出异常让上层重试
                if resp.status in (401, 403):
                    await self._mark_key_exhausted(api_key)
                    raise SerpApiQuotaExhaustedError(api_key, status=resp.status)
                logger.error(f"[GoogleLens] API 返回错误: HTTP {resp.status}")
                return []

            text = await resp.text()
            data = json.loads(text)

        # 检查响应中的错误
        if "error" in data:
            error_msg = data.get("error", "")
            if "API key" in error_msg or "exceeded" in error_msg.lower():
                await self._mark_key_exhausted(api_key)
                raise SerpApiQuotaExhaustedError(api_key, status=None)
            logger.error(f"[GoogleLens] SerpAPI 错误: {error_msg}")
            return []

        # 解析结果
        results: list[SearchResultItem] = []
        matches = data.get("visual_matches")
        if isinstance(matches, list):
            limit = min(len(matches), self.max_results)

            for i in range(limit):
                try:
                    match = matches[i]
                    if not isinstance(match, dict):
                        continue
                    title = match.get("title", "")
                    link = match.get("link", "")
                    source = match.get("source", "")
                    thumbnail = match.get("thumbnail", "")

                    if not title or not link:
                        continue

                    results.append(
                        SearchResultItem(
                            title=title,
                            url=link,
                            thumbnail=thumbnail,
                            thumbnail_bytes=None,  # 缩略图统一由 service 并行下载
                            source="Google Lens",
                            similarity=None,
                            description=source,
                            domain=None,
                            # Google Lens 不给相似度，只能用"位次越前越可信"近似，
                            # 再乘来源可信度系数，才能和 SauceNAO 的真实相似度同尺度排序。
                            score=positional_score(i, limit, SOURCE_KEY_GOOGLE_LENS),
                            source_key=SOURCE_KEY_GOOGLE_LENS,
                        )
                    )
                except Exception as e:
                    logger.warning(f"[GoogleLens] 解析结果项失败: {e}")

        logger.info(f"[GoogleLens] 搜索完成，获取 {len(results)} 条结果")
        return results

    async def _select_key_optimistically(self) -> str | None:
        """乐观选择 API Key，不预先检查额度.

        使用轮询方式选择 Key，额度错误在实际请求时处理。
        使用缓存避免短时间内重复检查已知耗尽的 Key。

        Returns:
            可用的 API Key，如果没有则返回 None
        """
        if not self.api_keys:
            return None

        async with self._key_lock:
            # 清理过期的缓存
            now = time.time()
            expired_keys = [
                k
                for k, (_, ts) in self._quota_cache.items()
                if now - ts > QUOTA_CACHE_TTL
            ]
            for k in expired_keys:
                del self._quota_cache[k]

            # 清理过期的惩罚记录
            expired_penalties = [
                k for k, until in self._key_penalty_until.items() if until <= now
            ]
            for k in expired_penalties:
                del self._key_penalty_until[k]

            start_idx = self._current_key_index % len(self.api_keys)
            penalized_fallback: tuple[int, str] | None = None

            for i in range(len(self.api_keys)):
                idx = (start_idx + i) % len(self.api_keys)
                key = self.api_keys[idx]

                # 检查缓存中是否有额度信息
                cached = self._quota_cache.get(key)
                if cached:
                    searches_left, _ = cached
                    if searches_left <= 0:
                        continue  # 缓存显示已耗尽，跳过

                if key in self._key_penalty_until:
                    # 冷却中的 Key 先记下来，其它 Key 都不可用时再退回来用
                    if penalized_fallback is None:
                        penalized_fallback = (idx, key)
                    continue

                # 选中后游标必须前移到下一个 Key：原来置为 idx 会导致同一次搜图的
                # 多轮重试反复挑到同一个坏 Key，多 Key 负载均衡完全失效。
                self._current_key_index = idx + 1
                return key

            if penalized_fallback is not None:
                idx, key = penalized_fallback
                self._current_key_index = idx + 1
                return key

            return None

    async def _penalize_key(self, api_key: str) -> None:
        """给失败的 API Key 记一段短时冷却.

        Args:
            api_key: 失败的 API Key
        """
        async with self._key_lock:
            self._key_penalty_until[api_key] = time.time() + SERPAPI_KEY_PENALTY_SECONDS
            logger.debug(
                f"[GoogleLens] Key ...{api_key[-4:]} 进入 "
                f"{SERPAPI_KEY_PENALTY_SECONDS}s 冷却"
            )

    async def _mark_key_exhausted(self, api_key: str) -> None:
        """标记 API Key 已耗尽.

        Args:
            api_key: 已耗尽的 API Key
        """
        async with self._key_lock:
            self._quota_cache[api_key] = (0, time.time())
            logger.debug(f"[GoogleLens] 已标记 Key ...{api_key[-4:]} 为耗尽状态")
