"""Yandex 图片反搜策略。

Yandex 没有稳定的公开 JSON 反搜接口，图片 URL 搜索会返回一个 HTML 页面，
结果通常保存在 ``div.Root[data-state]`` 的 JSON 中。本模块只使用标准库解析
这段状态，避免为了一个引擎引入 pyquery/lxml 等额外依赖。
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from html import unescape
from html.parser import HTMLParser
from typing import Any

import aiohttp
from astrbot.api import logger

from .constant import (
    DEFAULT_YANDEX_MAX_RESULTS,
    HTTP_TIMEOUT_SECONDS,
    IMAGE_DOWNLOAD_TIMEOUT,
    SOURCE_KEY_YANDEX,
    YANDEX_BASE_URL,
    YANDEX_RU_BASE_URL,
    YANDEX_SEARCH_PATH,
)
from .models import SearchResultItem
from .ranking import positional_score
from .strategy import ImageSearchStrategy
from .utils import (
    coerce_bool,
    coerce_int,
    download_bytes,
    get_aiohttp_session,
    get_proxy_url,
    get_user_agent,
)

_CAPTCHA_MARKERS = (
    "captcha",
    "smartcaptcha",
    "showcaptcha",
    "verify you are human",
    "проверка робота",
)
_WHITESPACE_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


class _YandexStateParser(HTMLParser):
    """提取 Yandex 结果根节点的 data-state 属性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.data_state: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.data_state is not None or tag.casefold() != "div":
            return
        attributes = dict(attrs)
        element_id = attributes.get("id") or ""
        # 当前页面使用 ImagesApp-<随机 ID>；保留 ImagesApp 本身作为结构变化时
        # 的宽容回退，避免页面只改了短横线就完全失效。
        if element_id.casefold().startswith("imagesapp"):
            state = attributes.get("data-state")
            if state:
                self.data_state = state


class YandexStrategy(ImageSearchStrategy):
    """通过 Yandex Images 的 HTML 页面执行图片 URL 反搜。"""

    def __init__(
        self,
        cookies: str | Mapping[str, Any] | None = None,
        max_results: int = DEFAULT_YANDEX_MAX_RESULTS,
        use_ru_fallback: bool = True,
        base_url: str = YANDEX_BASE_URL,
    ) -> None:
        """初始化 Yandex 策略。

        Args:
            cookies: 可选的 ``name=value; name2=value2`` Cookie 字符串，也接受
                字典形式。Cookie 不会写入日志。
            max_results: 单次最多保留的结果数。
            use_ru_fallback: .com 无法访问或被 CAPTCHA 拦截时是否重试 .ru。
            base_url: 首选 Yandex 站点，主要用于测试和地区定制。
        """
        self.cookies = self._parse_cookies(cookies)
        self.max_results = coerce_int(max_results, DEFAULT_YANDEX_MAX_RESULTS, 1, 30)
        self.use_ru_fallback = coerce_bool(use_ru_fallback, True)
        self.base_url = self._normalize_origin(base_url) or YANDEX_BASE_URL

    def get_service_name(self) -> str:
        return "Yandex"

    async def search(self, image_url: str) -> list[SearchResultItem]:
        """执行 Yandex 图片 URL 搜索。"""
        if not self._is_http_url(image_url):
            logger.warning("[Yandex] 仅支持 HTTP/HTTPS 图片 URL")
            return []

        for index, origin in enumerate(self._candidate_origins()):
            try:
                html, status = await self._request_html(origin, image_url)
            except Exception as error:
                # 不记录请求 URL 或 Cookie；异常字符串也主动脱敏，避免某些
                # HTTP 客户端把请求头回显到异常文本里。
                logger.warning(f"[Yandex] {self._host(origin)} 请求失败: {self._safe_error(error)}")
                continue

            if status != 200:
                logger.warning(f"[Yandex] {self._host(origin)} 返回 HTTP {status}")
                continue

            state = self._extract_data_state(html)
            if not state:
                if self._looks_like_captcha(html):
                    logger.warning(
                        f"[Yandex] {self._host(origin)} 返回 CAPTCHA 页面，"
                        "将尝试备用站点（如已开启）"
                    )
                else:
                    logger.warning(
                        f"[Yandex] {self._host(origin)} 响应缺少 data-state，页面结构可能已变化"
                    )
                continue

            results = self._parse_state(state)
            if results or index == len(self._candidate_origins()) - 1:
                logger.info(f"[Yandex] 搜索完成，获取 {len(results)} 条结果")
                return results
            # 有状态但 JSON 损坏时也允许 .com -> .ru 回退。
            logger.warning(f"[Yandex] {self._host(origin)} 结果状态无法解析")

        return []

    async def _request_html(self, origin: str, image_url: str) -> tuple[str, int]:
        """请求一个 Yandex 站点并返回页面文本与 HTTP 状态。"""
        session = await get_aiohttp_session()
        headers = {
            "User-Agent": get_user_agent(),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{origin}{YANDEX_SEARCH_PATH}",
        }
        params = {"rpt": "imageview", "url": image_url}
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        async with session.get(
            f"{origin}{YANDEX_SEARCH_PATH}",
            params=params,
            headers=headers,
            cookies=self.cookies or None,
            timeout=timeout,
            proxy=get_proxy_url(),
        ) as response:
            try:
                text = await response.text(errors="replace")
            except TypeError:
                # 兼容极简测试桩以及少数 aiohttp 兼容响应对象的 text() 签名。
                text = await response.text()
            return text, int(getattr(response, "status", 0) or 0)

    def _candidate_origins(self) -> tuple[str, ...]:
        """返回首选站点和可选的 .ru 回退站点，去除重复项。"""
        origins = [self.base_url]
        if self.use_ru_fallback and self._host(self.base_url) == "yandex.com":
            origins.append(YANDEX_RU_BASE_URL)
        return tuple(dict.fromkeys(origins))

    @classmethod
    def _extract_data_state(cls, html: str) -> str | None:
        """从 HTML 提取结果根节点的 data-state JSON 字符串。"""
        if not isinstance(html, str) or not html:
            return None
        parser = _YandexStateParser()
        try:
            parser.feed(html)
            parser.close()
        except Exception:
            return None
        if not parser.data_state:
            return None
        return unescape(parser.data_state)

    @classmethod
    def _parse_html(
        cls, html: str, max_results: int = DEFAULT_YANDEX_MAX_RESULTS
    ) -> list[SearchResultItem]:
        """解析完整 HTML；供单测和未来页面结构适配使用。"""
        state = cls._extract_data_state(html)
        if not state:
            return []
        parser = cls(max_results=max_results)
        return parser._parse_state(state)

    def _parse_state(self, state: str) -> list[SearchResultItem]:
        try:
            payload: Any = json.loads(unescape(state))
        except (TypeError, ValueError):
            return []

        initial_state = self._mapping_value(payload, "initialState")
        # 少数页面会把 initialState 再包成 JSON 字符串。
        if isinstance(initial_state, str):
            try:
                initial_state = json.loads(initial_state)
            except (TypeError, ValueError):
                return []
        cbir_sites = self._mapping_value(initial_state, "cbirSites")
        sites = self._mapping_value(cbir_sites, "sites")
        if not isinstance(sites, list):
            return []

        candidates: list[dict[str, str | None]] = []
        seen_urls: set[str] = set()
        for site in sites:
            if not isinstance(site, Mapping):
                continue
            url = self._http_url(site.get("url"))
            if not url:
                continue
            normalized = self._normalize_for_dedupe(url)
            if normalized in seen_urls:
                continue
            seen_urls.add(normalized)

            domain = self._clean_text(site.get("domain"))
            if not domain:
                domain = urllib.parse.urlsplit(url).hostname or ""
            title = self._clean_text(site.get("title")) or domain or "Yandex Result"
            description = self._clean_text(site.get("description")) or None
            if description:
                description = description[:500]

            thumb_data = site.get("thumb")
            if isinstance(thumb_data, Mapping):
                thumb_value = (
                    thumb_data.get("url") or thumb_data.get("href") or thumb_data.get("src")
                )
            else:
                thumb_value = thumb_data
            thumbnail = self._http_url(thumb_value)

            candidates.append(
                {
                    "title": title,
                    "url": url,
                    "thumbnail": thumbnail or "",
                    "description": description,
                    "domain": domain or None,
                }
            )

        total = len(candidates)
        results: list[SearchResultItem] = []
        for index, candidate in enumerate(candidates[: self.max_results]):
            results.append(
                SearchResultItem(
                    title=str(candidate["title"] or "Yandex Result"),
                    url=str(candidate["url"]),
                    thumbnail=str(candidate["thumbnail"] or ""),
                    thumbnail_bytes=None,
                    source="Yandex",
                    similarity=None,
                    description=candidate["description"],
                    domain=candidate["domain"],
                    score=positional_score(index, total, SOURCE_KEY_YANDEX),
                    source_key=SOURCE_KEY_YANDEX,
                )
            )
        return results

    async def fetch_thumbnail(self, url: str) -> bytes | None:
        """下载 Yandex 缩略图，并带 Referer；不向第三方域名发送 Yandex Cookie。"""
        normalized = self._http_url(url)
        if not normalized:
            return None
        parsed = urllib.parse.urlsplit(normalized)
        host = (parsed.hostname or "").casefold()
        origin = self._origin_for_host(host)
        headers = {"Referer": f"{origin}{YANDEX_SEARCH_PATH}"}

        # 只有 Yandex 自有 CDN 才携带配置 Cookie，避免恶意或被篡改的
        # 缩略图 URL 借机窃取凭据；普通外链走共享下载器。
        if not self._is_yandex_host(host) or not self.cookies:
            return await download_bytes(normalized, timeout=IMAGE_DOWNLOAD_TIMEOUT, headers=headers)

        try:
            session = await get_aiohttp_session()
            timeout = aiohttp.ClientTimeout(total=IMAGE_DOWNLOAD_TIMEOUT)
            async with session.get(
                normalized,
                headers={"User-Agent": get_user_agent(), **headers},
                cookies=self.cookies,
                timeout=timeout,
                proxy=get_proxy_url(),
            ) as response:
                if getattr(response, "status", 0) != 200:
                    return None
                return await response.read()
        except Exception as error:
            logger.debug(f"[Yandex] 缩略图下载失败: {self._safe_error(error)}")
            return None

    @staticmethod
    def _mapping_value(value: Any, key: str) -> Any:
        return value.get(key) if isinstance(value, Mapping) else None

    @staticmethod
    def _parse_cookies(value: str | Mapping[str, Any] | None) -> dict[str, str]:
        if isinstance(value, Mapping):
            return {
                str(key).strip(): str(item).strip()
                for key, item in value.items()
                if str(key).strip() and item is not None and str(item).strip()
            }
        if not isinstance(value, str):
            return {}
        text = value.strip()
        if not text:
            return {}
        # 兼容 WebUI 粘贴的 JSON 对象。
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, Mapping):
                return YandexStrategy._parse_cookies(parsed)

        cookies: dict[str, str] = {}
        for chunk in text.split(";"):
            if "=" not in chunk:
                continue
            name, item = chunk.split("=", 1)
            name, item = name.strip(), item.strip()
            if name and item and "\r" not in name + item and "\n" not in name + item:
                cookies[name] = item
        return cookies

    @staticmethod
    def _clean_text(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        text = _TAG_RE.sub(" ", unescape(value))
        return _WHITESPACE_RE.sub(" ", text).strip()

    @classmethod
    def _http_url(cls, value: Any) -> str:
        if not isinstance(value, str):
            return ""
        url = unescape(value).strip()
        if url.startswith("//"):
            url = "https:" + url
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return ""
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
            return ""
        return url

    @classmethod
    def _is_http_url(cls, value: Any) -> bool:
        return bool(cls._http_url(value))

    @staticmethod
    def _normalize_origin(value: str) -> str:
        if not isinstance(value, str):
            return ""
        text = value.strip().rstrip("/")
        try:
            parsed = urllib.parse.urlsplit(text)
        except ValueError:
            return ""
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @staticmethod
    def _host(origin: str) -> str:
        return (urllib.parse.urlsplit(origin).hostname or origin).casefold()

    @staticmethod
    def _is_yandex_host(host: str) -> bool:
        host = host.casefold().rstrip(".")
        return host in {"yandex.com", "yandex.ru"} or host.endswith(
            (".yandex.com", ".yandex.ru", ".yandex.net")
        )

    @classmethod
    def _origin_for_host(cls, host: str) -> str:
        return YANDEX_RU_BASE_URL if host.endswith(".ru") else YANDEX_BASE_URL

    @staticmethod
    def _normalize_for_dedupe(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit(
            (
                parsed.scheme.casefold(),
                parsed.netloc.casefold(),
                parsed.path.rstrip("/") or "/",
                parsed.query,
                "",
            )
        )

    @staticmethod
    def _looks_like_captcha(html: str) -> bool:
        lowered = html.casefold()
        return any(marker in lowered for marker in _CAPTCHA_MARKERS)

    def _safe_error(self, error: BaseException) -> str:
        message = str(error) or type(error).__name__
        for value in self.cookies.values():
            if value:
                message = message.replace(value, "***REDACTED***")
        return message[:300]
