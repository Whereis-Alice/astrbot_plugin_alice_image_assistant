"""搜图神器候选图的下载与九宫格拼图工具。

拼图上的编号是 VLM 选图的唯一定位依据，因此编号绘制与图片顺序必须保持稳定；
下载/解码失败不再静默丢弃，而是记录日志，便于排查"候选池被莫名削减导致选不中"的问题。
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import math
import socket
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps

TILE_SIZE = 300
MAX_IMAGE_SIZE = 15 * 1024 * 1024

_FONT_CACHE = None

# 下载失败可能成批出现，仅首条升级为 warning，其余降级 debug，避免日志被刷爆又不至于完全静默。
_DOWNLOAD_WARNED = False


def _log_download_failure(url: str, reason: str) -> None:
    """记录候选图下载失败原因（首条 warning，后续 debug）。"""
    global _DOWNLOAD_WARNED
    message = f"[AliceImageSoutu] 候选图下载失败，已跳过：{url} 原因：{reason}"
    if _DOWNLOAD_WARNED:
        logger.debug(message)
    else:
        _DOWNLOAD_WARNED = True
        logger.warning(message)


class SSRFInterceptError(Exception):
    pass


class SafeResolver(aiohttp.DefaultResolver):
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        resolved = await super().resolve(host, port, family)
        for info in resolved:
            ip_str = info["host"]
            try:
                ip = ipaddress.ip_address(ip_str)
                if (
                    ip.is_private
                    or ip.is_loopback
                    or ip.is_link_local
                    or ip.is_multicast
                    or getattr(ip, "is_reserved", False)
                    or ip.is_unspecified
                ):
                    raise SSRFInterceptError("检测到受限网络地址。")
            except ValueError:
                # 不是纯 IP（例如 DNS 返回了主机名形态）时无法做私网判定，记录后按放行处理。
                logger.debug(f"[AliceImageSoutu] 无法解析为 IP 地址，跳过私网校验：{ip_str}")
        return resolved


def is_safe_url_host(url: str) -> bool:
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        try:
            ip = ipaddress.ip_address(host)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or getattr(ip, "is_reserved", False)
                or ip.is_unspecified
            ):
                return False
        except ValueError:
            # host 是域名而非 IP，交给 SafeResolver 在真正连接时再做私网拦截。
            logger.debug(f"[AliceImageSoutu] URL 主机名不是 IP，延后校验：{host}")
        return True
    except Exception as exc:
        logger.warning(f"[AliceImageSoutu] URL 安全校验异常，按不安全处理：{url} 原因：{exc}")
        return False


def _get_large_font() -> ImageFont.ImageFont:
    global _FONT_CACHE
    if _FONT_CACHE is None:
        try:
            _FONT_CACHE = ImageFont.truetype("arial.ttf", 36)
        except OSError:
            try:
                _FONT_CACHE = ImageFont.truetype("DejaVuSans.ttf", 36)
            except OSError:
                _FONT_CACHE = ImageFont.load_default()
    return _FONT_CACHE


def _create_collage_sync(
    items: list[tuple[str, bytes]],
) -> tuple[bytes | None, list[tuple[str, bytes]]]:
    successful_images, valid_items = [], []
    for url, img_bytes in items:
        try:
            with Image.open(io.BytesIO(img_bytes)) as img:
                converted_img = ImageOps.fit(
                    img.convert("RGB"),
                    (TILE_SIZE, TILE_SIZE),
                    method=Image.Resampling.LANCZOS,
                )
                successful_images.append(converted_img)
                valid_items.append((url, img_bytes))
        except Exception as exc:
            # 坏图会让编号与候选错位，必须记录下来才能定位"编号对不上"的选图偏差。
            logger.debug(f"[AliceImageSoutu] 拼图跳过无法解码的候选图：{url} 原因：{exc}")
            continue

    if not successful_images:
        return None, []

    columns = math.ceil(math.sqrt(len(successful_images)))
    rows = math.ceil(len(successful_images) / columns)

    collage = Image.new("RGB", (columns * TILE_SIZE, rows * TILE_SIZE), (255, 255, 255))
    draw = ImageDraw.Draw(collage)
    font = _get_large_font()
    is_default_font = getattr(font, "size", None) is None

    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x_offset, y_offset = col * TILE_SIZE, row * TILE_SIZE
        collage.paste(img, (x_offset, y_offset))

        bg_box = [x_offset + 5, y_offset + 5, x_offset + 60, y_offset + 50]
        draw.rectangle(bg_box, fill="black")

        if is_default_font:
            txt_img = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
            ImageDraw.Draw(txt_img).text((0, 0), str(i + 1), fill="white", font=font)
            txt_img = txt_img.resize((80, 40), Image.Resampling.NEAREST)
            collage.paste(txt_img, (x_offset + 10, y_offset + 10), txt_img)
        else:
            draw.text(
                (x_offset + 15, y_offset + 10), str(i + 1), fill="white", font=font
            )

    with io.BytesIO() as buffer:
        collage.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue(), valid_items


class ComposerManager:
    """管理候选图下载会话与九宫格拼图。"""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._lock: asyncio.Lock | None = None

    def _ensure_primitives(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(15)

    async def _get_session(self) -> aiohttp.ClientSession:
        self._ensure_primitives()
        async with self._lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(resolver=SafeResolver())
                self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def close_all(self) -> None:
        self._ensure_primitives()
        async with self._lock:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
            self._semaphore = None

    async def _download_image(self, url: str) -> tuple[str, bytes | None]:
        if not is_safe_url_host(url):
            _log_download_failure(url, "主机名未通过安全校验")
            return url, None

        self._ensure_primitives()
        session = await self._get_session()

        async with self._semaphore:
            referer = "https://www.soutushenqi.com/"
            if any(x in url for x in ["huaban.com", "hb.aicdn.com", "hbimg"]):
                referer = "https://huaban.com/"
            elif "duitang.com" in url:
                referer = "https://www.duitang.com/"
            elif any(x in url for x in ["hdslb.com", "bilibili.com"]):
                referer = "https://www.bilibili.com/"
            elif "sinaimg.cn" in url:
                referer = ""
            elif "zhimg.com" in url:
                referer = "https://www.zhihu.com/"
            elif "gamersky.com" in url:
                referer = ""
            elif "douyinpic.com" in url:
                referer = "https://www.douyin.com/"
            elif "baidu.com" in url:
                referer = "https://image.baidu.com/"

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            }
            if referer:
                headers["Referer"] = referer

            req_timeout = aiohttp.ClientTimeout(connect=5, sock_read=8)
            try:
                async with session.get(
                    url, headers=headers, timeout=req_timeout, allow_redirects=False
                ) as resp:
                    if resp.status != 200:
                        _log_download_failure(url, f"HTTP {resp.status}")
                        return url, None
                    content_type = resp.headers.get("Content-Type", "").lower()

                    if not content_type.startswith("image/"):
                        _log_download_failure(url, f"Content-Type 非图片：{content_type}")
                        return url, None

                    chunks: list[bytes] = []
                    downloaded_size = 0
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        downloaded_size += len(chunk)
                        if downloaded_size > MAX_IMAGE_SIZE:
                            _log_download_failure(url, "图片体积超过上限")
                            return url, None
                        chunks.append(chunk)
                    return url, b"".join(chunks)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log_download_failure(url, str(exc) or type(exc).__name__)
                return url, None

    async def download_image_batch(
        self, urls: list[str], target_count: int = 9
    ) -> list[tuple[str, bytes]]:
        valid_items: list[tuple[str, bytes]] = []
        pending_tasks = [asyncio.create_task(self._download_image(url)) for url in urls]

        try:
            for coro in asyncio.as_completed(pending_tasks):
                if len(valid_items) >= target_count:
                    for t in pending_tasks:
                        if not t.done():
                            t.cancel()
                    break

                try:
                    url, res = await coro
                    if isinstance(res, bytes) and res:
                        valid_items.append((url, res))
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.debug(f"[AliceImageSoutu] 批量下载单任务异常，已跳过：{exc}")
                    continue
        finally:
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

        input_order = {url: index for index, url in enumerate(urls)}
        valid_items.sort(key=lambda item: input_order.get(item[0], len(urls)))
        return valid_items

    async def create_collage_from_items(
        self, items: list[tuple[str, bytes]]
    ) -> tuple[bytes | None, list[tuple[str, bytes]]]:
        """把候选图拼成带编号的九宫格，返回拼图与实际参与拼图的候选（保持编号一致）。"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _create_collage_sync, items)
