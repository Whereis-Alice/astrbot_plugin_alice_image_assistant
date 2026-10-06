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
    SOURCE_KEY_GOOGLE_LENS,
    SOURCE_KEY_GOOGLE_LENS_EXACT,
)
from .models import ProviderSearchError, SearchResultItem
from .ranking import dedupe_by_url, positional_score
from .strategy import ImageSearchStrategy
from .utils import (
    coerce_bool,
    coerce_int,
    get_aiohttp_session,
    get_proxy_url,
    normalize_credentials,
)

# 额度缓存 TTL（秒）
QUOTA_CACHE_TTL = 60


class SerpApiQuotaExhaustedError(RuntimeError):
    """SerpAPI Key 额度耗尽异常."""

    def __init__(self, api_key: str, status: int | None = None) -> None:
        self.api_key = api_key
        self.status = status
        super().__init__(f"SerpAPI key unavailable (status={status})")


class GoogleLensStrategy(ImageSearchStrategy):
    """Google Lens 搜图策略.

    使用 SerpAPI 的 google_lens 引擎进行图片搜索。
    支持多 API Key 负载均衡和余额检查。
    """

    def __init__(
        self,
        api_keys: list[str] | None = None,
        max_results: int = DEFAULT_GOOGLE_LENS_MAX_RESULTS,
        *,
        search_type: str = "all",
        language: str = "zh-cn",
        country: str = "",
        auto_crop: bool = False,
    ) -> None:
        """初始化 Google Lens 策略.

        Args:
            api_keys: SerpAPI API Key 列表，支持多 Key 负载均衡
            max_results: 去重后单次取用的匹配条数
        """
        self.api_keys = normalize_credentials(api_keys)
        # WebUI 传来的数值可能是字符串，先强制转换再夹取，避免初始化期崩溃
        self.max_results = coerce_int(max_results, DEFAULT_GOOGLE_LENS_MAX_RESULTS, 1, 30)
        self.search_type = search_type if search_type in {"all", "exact_matches", "visual_matches", "products"} else "all"
        self.language = str(language or "zh-cn").strip()[:20]
        self.country = str(country or "").strip()[:5]
        self.auto_crop = coerce_bool(auto_crop, False)
        self._current_key_index = 0
        self._key_lock = asyncio.Lock()
        # 额度缓存: {api_key: (searches_left, timestamp)}
        self._quota_cache: dict[str, tuple[int, float]] = {}

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
            raise ProviderSearchError("未配置 Google Lens Key")

        if not image_url.startswith(("http://", "https://")):
            logger.warning("[GoogleLens] SerpAPI 不支持本地文件")
            raise ProviderSearchError("Google Lens 仅支持 HTTP 图片 URL")

        # Only key-specific errors justify another key; network/server failures do not.
        for _ in range(len(self.api_keys)):
            try:
                return await self._search_with_key(image_url)
            except SerpApiQuotaExhaustedError as exc:
                if not exc.api_key:
                    break
                logger.warning("[GoogleLens] 当前 Key 鉴权或额度受限，尝试其它 Key")
            except Exception as exc:
                raise ProviderSearchError(f"Google Lens 搜索服务不可用：{type(exc).__name__}") from exc
        raise ProviderSearchError("Google Lens 搜索服务不可用：没有剩余的可用 Key")

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

        return await self._request_with_key(api_key, image_url)

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
            "type": self.search_type,
            "hl": self.language,
            "auto_crop": str(self.auto_crop).lower(),
        }
        if self.country:
            params["country"] = self.country

        url = f"{SERPAPI_BASE_URL}/search?{urllib.parse.urlencode(params)}"

        session = await get_aiohttp_session()
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        proxy = get_proxy_url()

        async with session.get(url, timeout=timeout, proxy=proxy) as resp:
            if resp.status != 200:
                # 处理额度耗尽错误：标记当前 key 耗尽，并抛出异常让上层重试
                if resp.status in (401, 403, 429):
                    await self._mark_key_exhausted(api_key)
                    raise SerpApiQuotaExhaustedError(api_key, status=resp.status)
                raise RuntimeError(f"Google Lens HTTP {resp.status}")

            text = await resp.text()
            data = json.loads(text)

        # 检查响应中的错误
        if not isinstance(data, dict):
            raise ValueError("Google Lens 返回了非对象响应")
        if "error" in data:
            error_msg = str(data.get("error") or "")
            if "hasn't returned any results" in error_msg.lower():
                logger.info("[GoogleLens] SerpAPI 未返回匹配结果")
                return []
            if "API key" in error_msg or "exceeded" in error_msg.lower():
                await self._mark_key_exhausted(api_key)
                raise SerpApiQuotaExhaustedError(api_key, status=None)
            raise RuntimeError("Google Lens API 返回错误")

        # 解析结果
        results: list[SearchResultItem] = []
        # exact_matches 是 Google 判定为同图/同来源的强证据，排在 visual_matches 之前。
        # 跳过脏项并合并重复链接后再截断，保留后续的有效线索。
        match_groups = (
            ("exact_matches", SOURCE_KEY_GOOGLE_LENS_EXACT),
            ("visual_matches", SOURCE_KEY_GOOGLE_LENS),
        )
        if self.search_type == "exact_matches":
            match_groups = match_groups[:1]
        elif self.search_type in {"visual_matches", "products"}:
            match_groups = match_groups[1:]
        for group_name, source_key in match_groups:
            matches = data.get(group_name)
            if not isinstance(matches, list):
                continue
            valid_matches = [
                match
                for match in matches
                if isinstance(match, dict)
                and isinstance(match.get("title"), str)
                and bool(match.get("title", "").strip())
                and isinstance(match.get("link"), str)
                and match.get("link", "").startswith(("https://", "http://"))
            ]
            limit = len(valid_matches)

            for i, match in enumerate(valid_matches[:limit]):
                try:
                    title = match.get("title", "")
                    link = match.get("link", "")
                    source = str(match.get("source") or "")
                    snippet = str(match.get("snippet") or match.get("description") or "")
                    thumbnail = match.get("thumbnail", "")
                    if not isinstance(thumbnail, str) or not thumbnail.startswith(("https://", "http://")):
                        thumbnail = ""

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
                            description=" · ".join(part for part in (source, snippet) if part)[:800],
                            domain=urllib.parse.urlsplit(link).hostname,
                            # 位次仅是启发式排序依据，不是识别正确率。
                            score=positional_score(i, limit, source_key),
                            source_key=source_key,
                        )
                    )
                except Exception as e:
                    logger.warning(f"[GoogleLens] 解析结果项失败: {type(e).__name__}")

        logger.info(f"[GoogleLens] 搜索完成，获取 {len(results)} 条结果")
        return dedupe_by_url(results)[: self.max_results]

    async def _select_key_optimistically(self) -> str | None:
        """Round-robin valid keys without penalizing unrelated network failures."""
        if not self.api_keys:
            return None
        async with self._key_lock:
            now = time.monotonic()
            self._quota_cache = {key: value for key, value in self._quota_cache.items()
                                 if now - value[1] <= QUOTA_CACHE_TTL}
            for _ in range(len(self.api_keys)):
                index = self._current_key_index % len(self.api_keys)
                self._current_key_index = index + 1
                key = self.api_keys[index]
                if self._quota_cache.get(key, (1, now))[0] > 0:
                    return key
            return None

    async def _mark_key_exhausted(self, api_key: str) -> None:
        """标记 API Key 已耗尽.

        Args:
            api_key: 已耗尽的 API Key
        """
        async with self._key_lock:
            self._quota_cache[api_key] = (0, time.monotonic())
            logger.debug("[GoogleLens] 已缓存 Key 的鉴权/额度受限状态")
