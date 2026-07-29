"""图片下载与网格图拼接：异步下载候选图，Pillow 拼成带编号网格图供 VLM 比较。"""

from __future__ import annotations

import asyncio
import io
import math
import ssl

import aiohttp
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from astrbot.api import logger

from .image_utils import HttpService


def _insecure_ssl_context() -> ssl.SSLContext:
    """构造不校验证书的 SSL 上下文（仅用于证书不规范图源的降级重试）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def download_image(url: str, http: HttpService, retries: int = 2) -> bytes | None:
    """异步下载单张图片（带重试），失败返回 None。

    默认校验证书；仅在该 URL 出现 SSL 证书错误时对它单独降级为不校验重试，
    把"关闭校验"的暴露面收敛到确有证书问题的图源，而非一律不校验。
    """
    last_exception = None
    verify_ssl = True
    timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
    session = await http.session()

    for attempt in range(retries):
        try:
            ssl_arg = True if verify_ssl else _insecure_ssl_context()
            async with session.get(
                url,
                proxy=http.proxy_url,
                headers={"User-Agent": http.user_agent},
                timeout=timeout,
                ssl=ssl_arg,
            ) as resp:
                resp.raise_for_status()
                return await resp.read()
        except aiohttp.ClientSSLError as e:
            last_exception = e
            if verify_ssl:
                logger.debug(
                    f"[alice_image_serpapi] 证书校验失败，降级为不校验重试: {url}"
                )
                verify_ssl = False
                continue
            logger.debug(f"[alice_image_serpapi] 不校验仍 SSL 错误，跳过: {url}")
            return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_exception = e
            if isinstance(e, aiohttp.ClientResponseError) and e.status in (403, 404):
                logger.debug(f"[alice_image_serpapi] 下载遇到 {e.status}，跳过: {url}")
                return None
            logger.debug(
                f"[alice_image_serpapi] 下载第 {attempt + 1}/{retries} 次失败: {url}: {e}"
            )
            if attempt < retries - 1:
                await asyncio.sleep(1)

    logger.debug(
        f"[alice_image_serpapi] 下载全部失败: {url}. 最后错误: {last_exception}"
    )
    return None


def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """尝试加载可用字体，失败回退默认字体（尽量带上字号）。"""
    for font_name in (
        "Arial.ttf",
        "msyh.ttc",
        "simsun.ttc",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(font_name, size)
        except (IOError, OSError):
            continue
    try:
        # Pillow ≥10.1 支持给位图默认字体指定字号，编号更清晰
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _create_collage_sync(
    image_bytes_list: list[bytes], original_urls: list[str]
) -> tuple[bytes | None, list[str]]:
    """同步拼接网格图（放入线程池执行，避免阻塞事件循环）。"""
    successful_images: list[Image.Image] = []
    successful_urls: list[str] = []
    tile_size = 256

    for i, img_bytes in enumerate(image_bytes_list):
        if not img_bytes:
            continue
        try:
            img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            img = img.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            successful_images.append(img)
            successful_urls.append(original_urls[i])
        except (IOError, UnidentifiedImageError, Image.DecompressionBombError) as e:
            logger.debug(
                f"[alice_image_serpapi] 跳过无法处理的图片 {original_urls[i]}: {e}"
            )
            continue

    if not successful_images:
        return None, []

    columns = 4
    rows = math.ceil(len(successful_images) / columns)
    collage = Image.new("RGB", (columns * tile_size, rows * tile_size), (255, 255, 255))
    draw = ImageDraw.Draw(collage)
    font = _get_font(24)

    for i, img in enumerate(successful_images):
        row, col = i // columns, i % columns
        x_offset, y_offset = col * tile_size, row * tile_size
        collage.paste(img, (x_offset, y_offset))

        label = str(i + 1)
        draw.rectangle(
            [x_offset + 5, y_offset + 5, x_offset + 30, y_offset + 30], fill="black"
        )
        draw.text((x_offset + 8, y_offset + 8), label, fill="white", font=font)

    # 源图已贴入拼图，释放其解码资源（拼图本身已持有像素副本）
    for img in successful_images:
        img.close()

    buffer = io.BytesIO()
    collage.save(buffer, format="PNG")
    return buffer.getvalue(), successful_urls


async def create_collage(
    image_urls: list[str],
    http: HttpService,
) -> tuple[bytes | None, list[str]]:
    """并发下载图片并拼接网格图，返回 (拼接图字节, 成功对应的原始 URL 列表)。"""
    results = await asyncio.gather(
        *[download_image(url, http) for url in image_urls],
        return_exceptions=True,
    )

    successful = [
        (b, u)
        for b, u in zip(results, image_urls, strict=True)
        if isinstance(b, bytes) and b
    ]
    if not successful:
        return None, []

    image_bytes_list, original_urls = zip(*successful, strict=True)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _create_collage_sync, list(image_bytes_list), list(original_urls)
    )
