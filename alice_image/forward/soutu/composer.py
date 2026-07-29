# -*- coding: utf-8 -*-
import io
import math
import asyncio
import aiohttp
import socket
import ipaddress
from urllib.parse import urlparse
from typing import Optional, List, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

TILE_SIZE = 300
MAX_IMAGE_SIZE = 15 * 1024 * 1024

_FONT_CACHE = None


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
                pass
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
            pass
        return True
    except Exception:
        return False


def _get_large_font():
    global _FONT_CACHE
    if _FONT_CACHE is None:
        try:
            _FONT_CACHE = ImageFont.truetype("arial.ttf", 36)
        except IOError:
            try:
                _FONT_CACHE = ImageFont.truetype("DejaVuSans.ttf", 36)
            except IOError:
                _FONT_CACHE = ImageFont.load_default()
    return _FONT_CACHE


def _create_collage_sync(
    items: List[Tuple[str, bytes]],
) -> Tuple[Optional[bytes], List[Tuple[str, bytes]]]:
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
        except Exception:
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
    def __init__(self):
        self._session = None
        self._semaphore = None
        self._lock = None

    def _ensure_primitives(self):
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

    async def close_all(self):
        self._ensure_primitives()
        async with self._lock:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
            self._semaphore = None

    async def _download_image(self, url: str) -> Tuple[str, Optional[bytes]]:
        if not is_safe_url_host(url):
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
                        return url, None
                    content_type = resp.headers.get("Content-Type", "").lower()

                    if not content_type.startswith("image/"):
                        return url, None

                    chunks = []
                    downloaded_size = 0
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        downloaded_size += len(chunk)
                        if downloaded_size > MAX_IMAGE_SIZE:
                            return url, None
                        chunks.append(chunk)
                    return url, b"".join(chunks)
            except Exception:
                return url, None

    async def download_image_batch(
        self, urls: List[str], target_count: int = 9
    ) -> List[Tuple[str, bytes]]:
        valid_items = []
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
                except Exception:
                    continue
        finally:
            if pending_tasks:
                await asyncio.gather(*pending_tasks, return_exceptions=True)

        return valid_items

    # 🚀 补回遗失的救命方法，供 main.py 异步调用
    async def create_collage_from_items(
        self, items: List[Tuple[str, bytes]]
    ) -> Tuple[Optional[bytes], List[Tuple[str, bytes]]]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _create_collage_sync, items)
