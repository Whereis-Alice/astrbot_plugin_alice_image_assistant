"""搜图神器主图源抓取（Playwright 拦截 XHR）与 Bing 备用图源抓取。"""

from __future__ import annotations

import asyncio
import random
import re
import urllib.parse

import aiohttp
from astrbot.api import logger

try:  # Playwright 属于可选运行时依赖，缺失时必须给出可操作的提示而不是抛裸异常。
    from playwright.async_api import (
        Browser,
        async_playwright,
    )
    from playwright.async_api import (
        TimeoutError as PlaywrightTimeoutError,
    )

    PLAYWRIGHT_IMPORT_ERROR = ""
except Exception as _playwright_import_error:
    Browser = object  # type: ignore[assignment,misc]
    async_playwright = None  # type: ignore[assignment]
    PLAYWRIGHT_IMPORT_ERROR = str(_playwright_import_error)

    class PlaywrightTimeoutError(Exception):  # type: ignore[no-redef]
        """Playwright 不可用时的占位超时异常，保证 except 分支仍可编译。"""


# 统一的安装提示：抓取失败最常见的根因就是浏览器内核没装，直接把命令告诉用户。
PLAYWRIGHT_INSTALL_HINT = (
    "Playwright 未安装或浏览器内核不可用，请在插件运行环境中执行 "
    "pip install playwright 后再执行 playwright install chromium，然后重启 AstrBot。"
)

PLAYWRIGHT_TIMEOUT = 15000
SCROLL_TIMES = 3
SCROLL_WAIT = 2000

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]


def is_valid_image_url(u: str) -> bool:
    low_u = u.lower()
    if not low_u.startswith("http"):
        return False
    if "/assets/" in low_u or "favicon" in low_u:
        return False
    invalid_exts = [
        ".js",
        ".css",
        ".html",
        ".php",
        ".json",
        ".xml",
        ".ts",
        ".woff",
        ".ttf",
    ]
    if any(ext in low_u for ext in invalid_exts):
        return False
    blacklisted_domains = [
        "baidu.com",
        "bdimg.com",
        "bdstatic.com",
        "cnzz.com",
        "google-analytics.com",
    ]
    if any(domain in low_u for domain in blacklisted_domains):
        return False
    return not any(
        x in low_u for x in ["avatar", "logo", "icon", "qrcode", "profile", "banner"]
    )


def playwright_available() -> bool:
    """Playwright 是否可用；供上层在选源前做能力判断。"""
    return async_playwright is not None


class ScraperManager:
    def __init__(self) -> None:
        self._playwright_mgr: object | None = None
        self._browser: Browser | None = None
        self._session: aiohttp.ClientSession | None = None
        self._lock: asyncio.Lock | None = None

    def _ensure_primitives(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()

    async def _stop_playwright(self) -> None:
        """安全停止 playwright 管理器；失败必须留痕，否则句柄泄漏无从排查。"""
        if not self._playwright_mgr:
            return
        try:
            await asyncio.wait_for(self._playwright_mgr.stop(), timeout=5.0)
        except Exception as exc:
            logger.warning("[AliceImageSoutu] 停止 Playwright 管理器失败：%s", exc)
        finally:
            self._playwright_mgr = None

    async def _get_browser(self) -> Browser:
        self._ensure_primitives()
        if async_playwright is None:
            # 明确告知安装命令，而不是抛出 ModuleNotFoundError 让用户自己猜。
            detail = (
                " 原始错误：" + PLAYWRIGHT_IMPORT_ERROR
                if PLAYWRIGHT_IMPORT_ERROR
                else ""
            )
            raise RuntimeError(PLAYWRIGHT_INSTALL_HINT + detail)
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                await self._stop_playwright()

                try:
                    self._playwright_mgr = await asyncio.wait_for(
                        async_playwright().start(), timeout=10.0
                    )
                    self._browser = await asyncio.wait_for(
                        self._playwright_mgr.chromium.launch(
                            headless=True,
                            args=[
                                "--disable-blink-features=AutomationControlled",
                                "--no-sandbox",
                            ],
                        ),
                        timeout=25.0,
                    )
                except Exception as exc:
                    logger.error("[AliceImageSoutu] Playwright 初始化异常: %s", exc)
                    await self._stop_playwright()
                    self._browser = None
                    raise RuntimeError(
                        PLAYWRIGHT_INSTALL_HINT + " 原始错误：" + str(exc)
                    ) from exc
        return self._browser

    async def _get_session(self) -> aiohttp.ClientSession:
        self._ensure_primitives()
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
        return self._session

    async def close_all(self) -> None:
        self._ensure_primitives()
        async with self._lock:
            if self._browser:
                try:
                    await asyncio.wait_for(self._browser.close(), timeout=5.0)
                except Exception as exc:
                    logger.warning("[AliceImageSoutu] 关闭浏览器失败：%s", exc)
                finally:
                    self._browser = None
            await self._stop_playwright()
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None

    def _extract_bing_urls_sync(
        self, html: str, target_count: int, seen_urls: set
    ) -> list[str]:
        matches = re.findall(r'"murl"\s*:\s*"([^"]+)"', html.replace("&quot;", '"'))
        found = []
        for url in matches:
            if url not in seen_urls and is_valid_image_url(url):
                found.append(url)
                seen_urls.add(url)
                if len(found) >= target_count:
                    break
        return found

    async def fetch_bing_image_urls(self, keyword: str, target_count: int) -> list[str]:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        image_urls: list[str] = []
        seen_urls: set[str] = set()
        first, pages_fetched, max_pages = 0, 0, 10
        session = await self._get_session()
        loop = asyncio.get_running_loop()
        bing_timeout = aiohttp.ClientTimeout(total=15)

        while len(image_urls) < target_count and pages_fetched < max_pages:
            pages_fetched += 1
            url = f"https://www.bing.com/images/search?q={urllib.parse.quote(keyword)}&first={first}"
            try:
                async with session.get(
                    url, headers=headers, timeout=bing_timeout
                ) as resp:
                    if resp.status != 200:
                        break
                    html = await resp.text()
                    new_urls = await loop.run_in_executor(
                        None,
                        self._extract_bing_urls_sync,
                        html,
                        target_count - len(image_urls),
                        seen_urls,
                    )
                    if not new_urls:
                        break
                    image_urls.extend(new_urls)
                    first += 35
            except Exception as exc:
                logger.warning("[AliceImageSoutu] Bing 备用图源抓取失败：%s", exc)
                break
        return image_urls[:target_count]

    async def fetch_image_urls(
        self, keyword: str, target_count: int
    ) -> tuple[list[str], str]:
        valid_urls: list[str] = []
        seen_urls: set[str] = set()
        error_msg = ""
        context = None
        page = None
        # XHR 回调是高频路径，只对首次解析失败告警，其余降级为 debug，避免刷爆日志。
        parse_warned = [False]

        try:
            browser = await self._get_browser()
            context = await browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            page = await context.new_page()
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            async def handle_response(response) -> None:
                # 超限拦截器，防止无意义的内存追加和处理
                if len(valid_urls) >= target_count:
                    return

                if (
                    response.request.resource_type in ["fetch", "xhr"]
                    and response.status == 200
                ):
                    try:
                        if "image" in response.headers.get("content-type", ""):
                            return
                        json_data = await response.json()
                        if not json_data:
                            return
                        if "data" in json_data and isinstance(json_data["data"], list):
                            for item in json_data["data"]:
                                if len(valid_urls) >= target_count:
                                    break
                                large_url = item.get("largeUrl")
                                width = item.get("width", 0)
                                if (
                                    large_url
                                    and isinstance(large_url, str)
                                    and large_url.startswith("http")
                                    and width > 400
                                    and is_valid_image_url(large_url)
                                    and large_url not in seen_urls
                                ):
                                    seen_urls.add(large_url)
                                    valid_urls.append(large_url)
                    except Exception as exc:
                        if parse_warned[0]:
                            logger.debug(
                                "[AliceImageSoutu] 忽略无法解析的 XHR 响应：%s", exc
                            )
                        else:
                            parse_warned[0] = True
                            logger.warning(
                                "[AliceImageSoutu] 解析主图源 XHR 响应失败（后续同类错误降级为 debug）：%s",
                                exc,
                            )

            page.on("response", handle_response)

            search_url = f"https://www.soutushenqi.com/image/search?searchWord={urllib.parse.quote(keyword)}"
            logger.info(f"PicSearch: 开始截胡 [{keyword}] 的 API 数据...")

            try:
                await page.goto(
                    search_url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT
                )
            except PlaywrightTimeoutError as exc:
                # networkidle 超时是常态（页面长连接不断流），已拦截到的数据仍然可用。
                logger.debug("[AliceImageSoutu] 主图源页面加载超时，继续使用已拦截数据：%s", exc)

            await page.wait_for_timeout(1000)
            for _ in range(SCROLL_TIMES):
                if len(valid_urls) >= target_count:
                    break
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(SCROLL_WAIT)

            logger.info(f"PicSearch: 主图源 API 成功拦截 {len(valid_urls)} 张直链。")
            if not valid_urls:
                error_msg = "未能在网络流中拦截到任何高清大图数据。"

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("[AliceImageSoutu] 抓取管线发生异常: %s", exc)
            error_msg = f"抓取异常: {exc}"
        finally:
            if page:
                try:
                    await asyncio.wait_for(page.close(), timeout=2.0)
                except Exception as exc:
                    logger.warning("[AliceImageSoutu] 关闭页面失败：%s", exc)
            if context:
                try:
                    await asyncio.wait_for(context.close(), timeout=2.0)
                except Exception as exc:
                    logger.warning("[AliceImageSoutu] 关闭浏览器上下文失败：%s", exc)

        return valid_urls[:target_count], error_msg
