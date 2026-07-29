# -*- coding: utf-8 -*-
import urllib.parse
import asyncio
import aiohttp
import random
import re
from typing import List, Tuple
from playwright.async_api import (
    async_playwright,
    Browser,
    TimeoutError as PlaywrightTimeoutError,
)
from astrbot.api import logger

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
    if any(
        x in low_u for x in ["avatar", "logo", "icon", "qrcode", "profile", "banner"]
    ):
        return False
    return True


class ScraperManager:
    def __init__(self):
        self._playwright_mgr = None
        self._browser: Browser = None
        self._session = None
        self._lock = None

    def _ensure_primitives(self):
        if self._lock is None:
            self._lock = asyncio.Lock()

    async def _get_browser(self) -> Browser:
        self._ensure_primitives()
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._playwright_mgr:
                    try:
                        await self._playwright_mgr.stop()
                    except Exception:
                        pass

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
                except Exception as e:
                    logger.error(f"Playwright 初始化异常: {e}")
                    if self._playwright_mgr:
                        try:
                            await self._playwright_mgr.stop()
                        except Exception:
                            pass
                        self._playwright_mgr = None
                    self._browser = None
                    raise
        return self._browser

    async def _get_session(self) -> aiohttp.ClientSession:
        self._ensure_primitives()
        async with self._lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
        return self._session

    async def close_all(self):
        self._ensure_primitives()
        async with self._lock:
            if self._browser:
                try:
                    await asyncio.wait_for(self._browser.close(), timeout=5.0)
                except Exception:
                    pass
                finally:
                    self._browser = None
            if self._playwright_mgr:
                try:
                    await asyncio.wait_for(self._playwright_mgr.stop(), timeout=5.0)
                except Exception:
                    pass
                finally:
                    self._playwright_mgr = None
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

    async def fetch_bing_image_urls(self, keyword: str, target_count: int) -> List[str]:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        image_urls = []
        seen_urls = set()
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
            except Exception as e:
                logger.debug(f"Bing 备用图源抓取遇到错误: {e}")
                break
        return image_urls[:target_count]

    async def fetch_image_urls(
        self, keyword: str, target_count: int
    ) -> Tuple[List[str], str]:
        valid_urls = []
        seen_urls = set()
        error_msg = ""
        context = None
        page = None

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

            async def handle_response(response):
                # 🚀 优化点：超限拦截器，防止无意义的内存追加和处理
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
                                    break  # 🚀 早停退出
                                large_url = item.get("largeUrl")
                                width = item.get("width", 0)
                                if (
                                    large_url
                                    and isinstance(large_url, str)
                                    and large_url.startswith("http")
                                    and width > 400
                                ):
                                    if (
                                        is_valid_image_url(large_url)
                                        and large_url not in seen_urls
                                    ):
                                        seen_urls.add(large_url)
                                        valid_urls.append(large_url)
                    except Exception:
                        pass

            page.on("response", handle_response)

            search_url = f"https://www.soutushenqi.com/image/search?searchWord={urllib.parse.quote(keyword)}"
            logger.info(f"PicSearch: 开始截胡 [{keyword}] 的 API 数据...")

            try:
                await page.goto(
                    search_url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT
                )
            except PlaywrightTimeoutError:
                pass

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
        except Exception as e:
            logger.error(f"抓取管线发生异常: {str(e)}")
            error_msg = f"抓取异常: {e}"
        finally:
            if page:
                try:
                    await asyncio.wait_for(page.close(), timeout=2.0)
                except Exception:
                    pass
            if context:
                try:
                    await asyncio.wait_for(context.close(), timeout=2.0)
                except Exception:
                    pass

        return valid_urls[:target_count], error_msg
