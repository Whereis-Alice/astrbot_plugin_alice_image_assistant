"""Ascii2d 搜图策略实现.

通过 Ascii2d 网站进行图片搜索，支持 color 和 bovw 两种搜索模式。
使用 curl_cffi 模拟浏览器 TLS 指纹绕过 Cloudflare。
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import urllib.parse

from astrbot.api import logger
from curl_cffi.requests import AsyncSession

from .constant import (
    ASCII2D_BASE_URL,
    ASCII2D_SEARCH_URI_URL,
    ASCII2D_TOKEN_TTL_SECONDS,
    DEFAULT_ASCII2D_MAX_RESULTS,
    HTTP_TIMEOUT_SECONDS,
    IMAGE_DOWNLOAD_TIMEOUT,
    SOURCE_KEY_ASCII2D_BOVW,
    SOURCE_KEY_ASCII2D_COLOR,
)
from .models import SearchResultItem
from .ranking import dedupe_by_url, positional_score
from .strategy import ImageSearchStrategy
from .utils import coerce_int, get_proxy_url

# item-box 块：从 <div class='row item-box'> 到紧随其后的 clearfix
ITEM_BOX_PATTERN = re.compile(
    r"<div\s+class=['\"]row\s+item-box['\"][^>]*>"
    r".*?<div\s+class=['\"]clearfix['\"]></div>",
    re.DOTALL,
)

# detail-box 块：一个 item-box 内可能有多个 detail-box (同一张图的多个来源)
DETAIL_BOX_PATTERN = re.compile(
    r"<div[^>]+class=['\"][^'\"]*detail-box[^'\"]*['\"][^>]*>(.*?)</div>",
    re.DOTALL,
)

# detail-box 内 h6 里的锚点：(href, 文本)
H6_ANCHOR_PATTERN = re.compile(
    r"<h6[^>]*>(.*?)</h6>",
    re.DOTALL,
)
ANCHOR_PATTERN = re.compile(
    r"<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
    re.DOTALL,
)
SMALL_ANCHOR_PATTERN = re.compile(
    r"<small[^>]*>.*?<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
    re.DOTALL,
)

# 缩略图 <img src=...>
IMG_SRC_PATTERN = re.compile(r"<img[^>]+src=['\"]([^'\"]+)['\"]")

# 去标签，取锚点纯文本
TAG_PATTERN = re.compile(r"<[^>]+>")


class Ascii2dStrategy(ImageSearchStrategy):
    """Ascii2d 搜图策略.

    支持 HTTP 链接搜索，返回 color 和 bovw 搜索结果。
    使用 curl_cffi 模拟 Chrome 浏览器 TLS 指纹。
    """

    # 模拟 Chrome 浏览器 (curl_cffi 支持的稳定版本)
    IMPERSONATE_BROWSER = "chrome120"

    def __init__(
        self,
        session_id: str | None = None,
        cf_clearance: str | None = None,
        max_results: int = DEFAULT_ASCII2D_MAX_RESULTS,
    ) -> None:
        """初始化 Ascii2d 策略.

        Args:
            session_id: Ascii2d 网站的 _session_id Cookie 值
            cf_clearance: Cloudflare cf_clearance Cookie 值，用于绕过 CF 验证
            max_results: 单次搜索返回条数 (bovw + color 去重后)
        """
        self.session_id = session_id or ""
        self.cf_clearance = cf_clearance or ""
        # WebUI 传来的数值可能是字符串，先强制转换再夹取，避免初始化期崩溃
        self.max_results = coerce_int(max_results, DEFAULT_ASCII2D_MAX_RESULTS, 1, 20)
        # 共享的 curl_cffi AsyncSession，避免重复创建连接
        self._session: AsyncSession | None = None
        self._session_lock = asyncio.Lock()
        # authenticity_token 缓存 (值, 获取时间)
        # 每次搜图都先请求一次主页会白白多一个 RTT，token 在有效期内可以复用
        self._token: str | None = None
        self._token_ts: float = 0.0
        self._token_lock = asyncio.Lock()

    async def _get_session(self) -> AsyncSession:
        """获取共享的 AsyncSession 实例.

        懒初始化，首次调用时创建，后续复用。

        Returns:
            共享的 AsyncSession 实例
        """
        if self._session is not None:
            return self._session

        async with self._session_lock:
            if self._session is None:
                proxies = self._get_proxies()
                self._session = AsyncSession(
                    impersonate=self.IMPERSONATE_BROWSER,
                    proxies=proxies,
                    timeout=HTTP_TIMEOUT_SECONDS,
                )
            return self._session

    async def close(self) -> None:
        """关闭共享的 AsyncSession 并清理资源."""
        async with self._session_lock:
            if self._session is not None:
                await self._session.close()
                self._session = None
                logger.debug("[Ascii2d] AsyncSession 已关闭")
        # token 与会话绑定 (依赖同一批 Cookie)，会话关闭后必须一起失效
        self._token = None
        self._token_ts = 0.0

    def _get_cookies(self) -> dict:
        """获取 Cookie 字典.

        Returns:
            Cookie 字典
        """
        cookies = {}
        if self.cf_clearance:
            cookies["cf_clearance"] = self.cf_clearance
        if self.session_id:
            cookies["_session_id"] = self.session_id
        return cookies

    def _get_proxies(self) -> dict | None:
        """获取代理配置.

        Returns:
            代理配置字典，无代理返回 None
        """
        proxy_url = get_proxy_url()
        if proxy_url:
            return {"http": proxy_url, "https": proxy_url}
        return None

    def get_service_name(self) -> str:
        return "Ascii2d"

    async def search(self, image_url: str) -> list[SearchResultItem]:
        """执行 Ascii2d 搜索.

        Args:
            image_url: 图片 URL 地址

        Returns:
            搜索结果列表
        """
        if not image_url.startswith(("http://", "https://")):
            logger.warning("[Ascii2d] 仅支持 HTTP URL")
            return []

        try:
            # 步骤 1: 获取 authenticity_token (命中缓存时不再请求主页)
            token = await self._fetch_authenticity_token()
            if not token:
                logger.error("[Ascii2d] 获取 token 失败")
                return []

            # 步骤 2: 提交搜索请求，获取结果页 URL
            result_url = await self._post_url_search(image_url, token)
            if not result_url:
                # 缓存的 token 可能已过期，强制刷新后再试一次
                token = await self._fetch_authenticity_token(force_refresh=True)
                result_url = (
                    await self._post_url_search(image_url, token) if token else None
                )
            if not result_url:
                logger.error("[Ascii2d] 搜索请求失败")
                return []

            # 步骤 3: 并行获取 color 和 bovw 结果
            color_results, bovw_results = await asyncio.gather(
                self._fetch_and_parse_result_page(result_url, is_bovw=False),
                self._fetch_and_parse_result_page(result_url, is_bovw=True),
            )

            # 合并结果：bovw (特征匹配) 比 color (配色匹配) 可信，放在前面；
            # 两种模式的命中高度重叠，必须按规范化 URL 去重，否则展示位被重复项吃光。
            combined = dedupe_by_url([*bovw_results, *color_results])
            final_results = combined[: self.max_results]

            logger.info(f"[Ascii2d] 搜索完成，获取 {len(final_results)} 条结果")
            return final_results

        except Exception as e:
            logger.error(f"[Ascii2d] 搜索异常: {e}")
            return []

    async def fetch_thumbnail(self, url: str) -> bytes | None:
        """下载 Ascii2d 缩略图.

        ascii2d 的缩略图同样受 Cloudflare 保护，用裸 aiohttp (无 TLS 指纹、
        无 Cookie、无 Referer) 很容易吃 403，因此复用本策略的 curl_cffi 会话。

        Args:
            url: 缩略图 URL

        Returns:
            图片字节数据，失败返回 None
        """
        if not url or not url.startswith(("http://", "https://")):
            return None

        try:
            session = await self._get_session()
            response = await session.get(
                url,
                cookies=self._get_cookies(),
                headers={"Referer": f"{ASCII2D_BASE_URL}/"},
                timeout=IMAGE_DOWNLOAD_TIMEOUT,
            )
            if response.status_code != 200:
                logger.debug(
                    f"[Ascii2d] 缩略图下载失败: HTTP {response.status_code}"
                )
                return None
            return response.content
        except Exception as e:
            logger.debug(f"[Ascii2d] 缩略图下载异常: {e}")
            return None

    async def _fetch_authenticity_token(
        self, force_refresh: bool = False
    ) -> str | None:
        """获取 authenticity_token (带 TTL 缓存).

        Args:
            force_refresh: True 表示忽略缓存强制重新抓取

        Returns:
            token 字符串，失败返回 None
        """
        now = time.monotonic()
        if (
            not force_refresh
            and self._token
            and now - self._token_ts < ASCII2D_TOKEN_TTL_SECONDS
        ):
            return self._token

        async with self._token_lock:
            # 双重检查：并发搜图时只让第一个请求真正去抓主页
            now = time.monotonic()
            if (
                not force_refresh
                and self._token
                and now - self._token_ts < ASCII2D_TOKEN_TTL_SECONDS
            ):
                return self._token

            token = await self._request_authenticity_token()
            if token:
                self._token = token
                self._token_ts = time.monotonic()
            else:
                self._token = None
                self._token_ts = 0.0
            return token

    async def _request_authenticity_token(self) -> str | None:
        """从 Ascii2d 主页抓取 authenticity_token.

        Returns:
            token 字符串，失败返回 None
        """
        cookies = self._get_cookies()

        try:
            session = await self._get_session()
            response = await session.get(
                ASCII2D_BASE_URL,
                cookies=cookies,
            )

            if response.status_code != 200:
                logger.warning(f"[Ascii2d] 获取主页失败: HTTP {response.status_code}")
                logger.debug(
                    f"[Ascii2d] 响应内容: {response.text[:500] if response.text else 'empty'}"
                )
                return None

            html = response.text

        except Exception as e:
            logger.error(f"[Ascii2d] 获取主页异常: {e}")
            return None

        # 解析 token - 尝试多种模式
        patterns = [
            r'name="authenticity_token"\s+value="([^"]+)"',
            r'value="([^"]+)"\s*name="authenticity_token"',
            r'authenticity_token"\s+value="([^"]+)"',
            r'<input[^>]*name="authenticity_token"[^>]*value="([^"]+)"',
            r'<input[^>]*value="([^"]+)"[^>]*name="authenticity_token"',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                logger.debug("[Ascii2d] 成功获取 authenticity_token")
                return match.group(1)

        logger.warning(
            "[Ascii2d] 未找到 authenticity_token，可能网页结构已变化或 Session ID 无效"
        )
        logger.debug(f"[Ascii2d] HTML 片段: {html[:500] if html else 'empty'}")
        return None

    async def _post_url_search(self, image_url: str, token: str) -> str | None:
        """提交 URL 搜索请求.

        Args:
            image_url: 图片 URL
            token: authenticity_token

        Returns:
            结果页 URL，失败返回 None
        """
        cookies = self._get_cookies()

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": ASCII2D_BASE_URL,
            "Referer": f"{ASCII2D_BASE_URL}/",
        }

        # 手动构建表单数据
        form_data = urllib.parse.urlencode(
            {
                "utf8": "✓",
                "authenticity_token": token,
                "uri": image_url,
                "search": "",
            }
        )

        try:
            session = await self._get_session()
            response = await session.post(
                ASCII2D_SEARCH_URI_URL,
                data=form_data,
                cookies=cookies,
                headers=headers,
                allow_redirects=True,
            )

            if response.status_code == 200:
                final_url = str(response.url)
                # 验证是否跳转到结果页
                if "/search/color/" in final_url or "/search/bovw/" in final_url:
                    logger.info(f"[Ascii2d] 搜索成功，结果页: {final_url}")
                    return final_url
                # 落在非结果页 (如被打回首页) 时返回该 URL，后续 color/bovw 两路会
                # 抓同一个页面并产出完全重复的结果，因此直接判定为失败。
                logger.warning(f"[Ascii2d] 重定向到非结果页，已放弃: {final_url}")
                return None

            logger.warning(f"[Ascii2d] POST 失败: HTTP {response.status_code}")
            logger.debug(
                f"[Ascii2d] POST 响应: {response.text[:500] if response.text else 'empty'}"
            )
            return None

        except Exception as e:
            logger.error(f"[Ascii2d] POST 请求异常: {e}")
            return None

    async def _fetch_and_parse_result_page(
        self, base_url: str, is_bovw: bool
    ) -> list[SearchResultItem]:
        """获取并解析结果页.

        Args:
            base_url: 结果页基础 URL
            is_bovw: 是否为 bovw 模式

        Returns:
            解析后的结果列表（不含缩略图字节）
        """
        # 构建目标 URL
        if is_bovw:
            target_url = base_url.replace("/color/", "/bovw/")
        elif "/color/" not in base_url:
            target_url = base_url.replace("/bovw/", "/color/")
        else:
            target_url = base_url

        cookies = self._get_cookies()
        # 模拟浏览器直接访问结果页（无 Referer）
        # 注意：curl_cffi 会在重定向时自动处理 Referer，不要手动设置
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        }

        try:
            session = await self._get_session()
            response = await session.get(
                target_url,
                cookies=cookies,
                headers=headers,
            )

            if response.status_code != 200:
                logger.warning(f"[Ascii2d] 获取结果页失败: HTTP {response.status_code}")
                return []

            html = response.text

        except Exception as e:
            logger.error(f"[Ascii2d] 获取结果页异常: {e}")
            return []

        source_key = SOURCE_KEY_ASCII2D_BOVW if is_bovw else SOURCE_KEY_ASCII2D_COLOR
        return self._parse_ascii2d_html(html, source_key)

    @classmethod
    def _parse_ascii2d_html(
        cls, html: str, source_key: str = SOURCE_KEY_ASCII2D_BOVW
    ) -> list[SearchResultItem]:
        """解析 Ascii2d HTML 结果页.

        Args:
            html: HTML 内容
            source_key: 归一化来源键 (ascii2d/bovw 或 ascii2d/color)

        Returns:
            解析后的结果列表
        """
        parsed: list[tuple[str, str, str]] = []

        for box in ITEM_BOX_PATTERN.findall(html):
            try:
                triple = cls._parse_item_box(box)
            except Exception as e:
                logger.debug(f"[Ascii2d] 解析单个 item 失败: {e}")
                continue
            # triple 为 None 表示这个 box 里没有任何外站链接。
            # 查询原图块正是这种"只有自站链接/没有链接"的块，按内容特征识别比
            # 无条件丢弃第一个 box 更稳：正则漏匹配时不会把最相关的首条命中丢掉。
            if triple is not None:
                parsed.append(triple)

        total = len(parsed)
        results: list[SearchResultItem] = []
        for index, (title, url, thumbnail) in enumerate(parsed):
            results.append(
                SearchResultItem(
                    title=title,
                    url=url,
                    thumbnail=thumbnail,
                    thumbnail_bytes=None,
                    source="Ascii2d",
                    similarity=None,
                    description=None,
                    domain=None,
                    # ascii2d 不给相似度，用位次 + 模式可信度折算成可跨引擎比较的分数
                    score=positional_score(index, total, source_key),
                    source_key=source_key,
                )
            )
        return results

    @classmethod
    def _parse_item_box(cls, box: str) -> tuple[str, str, str] | None:
        """解析单个 item-box，返回 (标题, 链接, 缩略图) 三元组.

        Args:
            box: item-box 的 HTML 片段

        Returns:
            三元组；该块没有外站链接 (如查询原图块) 时返回 None
        """
        thumbnail = cls._extract_thumbnail(box)

        # 逐个 detail-box 解析：链接与标题必须取自同一个 detail-box，
        # 否则"标题取第一个 h6、链接回退到任意 small>a"会拼出互不相干的组合。
        detail_boxes = DETAIL_BOX_PATTERN.findall(box)
        for detail in detail_boxes or [box]:
            found = cls._extract_link_and_title(detail)
            if found is not None:
                title, url = found
                return (title, url, thumbnail)
        return None

    @staticmethod
    def _extract_thumbnail(box: str) -> str:
        """提取 item-box 的缩略图 URL.

        Args:
            box: item-box 的 HTML 片段

        Returns:
            缩略图 URL，未找到返回空字符串
        """
        candidates = IMG_SRC_PATTERN.findall(box)
        if not candidates:
            return ""
        # detail-box 里的站点图标也是 <img>，优先取 /thumbnail/ 下的命中图，
        # 避免把 pixiv 图标当成结果缩略图。
        chosen = next((src for src in candidates if "/thumbnail/" in src), candidates[0])
        if chosen.startswith("http"):
            return chosen
        return f"{ASCII2D_BASE_URL}{chosen}"

    @staticmethod
    def _extract_link_and_title(detail: str) -> tuple[str, str] | None:
        """从单个 detail-box 中提取 (标题, 外站链接).

        Args:
            detail: detail-box 的 HTML 片段

        Returns:
            (标题, 链接)；该片段内没有外站链接时返回 None
        """

        def clean(text: str) -> str:
            return TAG_PATTERN.sub("", text).strip()

        def absolutize(href: str) -> str:
            url = href
            with contextlib.suppress(Exception):
                url = urllib.parse.unquote(url)
            return url

        # 优先 h6 内的锚点 (作品标题所在位置)，回退到同一 detail-box 内的 <small><a>
        candidate_groups: list[list[tuple[str, str]]] = []
        for h6_inner in H6_ANCHOR_PATTERN.findall(detail):
            candidate_groups.append(ANCHOR_PATTERN.findall(h6_inner))
        candidate_groups.append(SMALL_ANCHOR_PATTERN.findall(detail))
        candidate_groups.append(ANCHOR_PATTERN.findall(detail))

        for group in candidate_groups:
            for href, text in group:
                url = absolutize(href)
                # 只接受外站绝对链接：站内相对链接是 ascii2d 自己的页面，
                # 当成溯源结果会把用户导回搜索页。
                if not url.startswith("http"):
                    continue
                title = clean(text) or url
                return (title, url)
        return None
