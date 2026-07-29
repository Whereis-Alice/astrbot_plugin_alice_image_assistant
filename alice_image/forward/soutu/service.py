# -*- coding: utf-8 -*-
import io
import asyncio
from typing import Optional, List, Tuple

from PIL import Image, UnidentifiedImageError
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context
from astrbot.api.provider import Provider
from astrbot.api import logger

from .scraper import ScraperManager
from .composer import ComposerManager
from .vlm import select_best_image_index

JPEG_QUALITY = 85


class SoutuSearchService:
    def __init__(self, context: Context, config: dict = None):
        self.context = context
        self.config = config or {}

        self.scraper_mgr = ScraperManager()
        self.composer_mgr = ComposerManager()
        self._vlm_semaphore = asyncio.Semaphore(2)

    async def terminate(self):
        await self.scraper_mgr.close_all()
        await self.composer_mgr.close_all()
        logger.info("SouTuShenQi 插件资源回收完成。")

    async def _get_vlm_provider(self, event: AstrMessageEvent) -> Optional[Provider]:
        provider_id = self.config.get("vlm_provider_id", "")
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider:
                return provider

        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            curr_id = await self.context.get_current_chat_provider_id(umo)
            if curr_id:
                provider = self.context.get_provider_by_id(curr_id)
                if provider:
                    return provider

        return None

    def _validate_and_hash_sync(
        self, img_bytes: bytes, min_res: int
    ) -> Tuple[bool, str]:
        try:
            with Image.open(io.BytesIO(img_bytes)) as img:
                if img.width < min_res or img.height < min_res:
                    return False, ""
                img = img.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
                pixels = list(img.getdata())
                avg = sum(pixels) / len(pixels)
                bits = "".join(["1" if p > avg else "0" for p in pixels])
                return True, hex(int(bits, 2))[2:].zfill(16)
        except Exception:
            return False, ""

    async def _ensure_minimum_images(self, keyword: str) -> List[Tuple[str, bytes]]:
        try:
            raw_count = self.config.get("batch_size", 9)
            target_count = max(1, min(int(raw_count), 16))
        except (TypeError, ValueError):
            target_count = 9

        try:
            raw_res = self.config.get("min_resolution", 500)
            min_resolution = max(100, min(int(raw_res), 4000))
        except (TypeError, ValueError):
            min_resolution = 500

        valid_items = []
        seen_hashes = set()
        loop = asyncio.get_running_loop()

        urls: list[str] = []
        if self.config.get("primary_source_enabled", True):
            urls, _ = await self.scraper_mgr.fetch_image_urls(keyword, target_count * 4)
        url_pool = urls.copy() if urls else []

        while url_pool and len(valid_items) < target_count:
            needed = target_count - len(valid_items)
            batch_size = min(len(url_pool), max(needed, needed * 2))

            batch_urls = url_pool[:batch_size]
            url_pool = url_pool[batch_size:]

            downloaded = await self.composer_mgr.download_image_batch(
                batch_urls, target_count=len(batch_urls)
            )

            for url, img_bytes in downloaded:
                if len(valid_items) >= target_count:
                    break
                is_valid, b_hash = await loop.run_in_executor(
                    None, self._validate_and_hash_sync, img_bytes, min_resolution
                )
                if is_valid and b_hash not in seen_hashes:
                    valid_items.append((url, img_bytes))
                    seen_hashes.add(b_hash)

        logger.info(f"主图源处理完毕，当前高清去重有效图片数: {len(valid_items)}")

        if len(valid_items) < target_count and self.config.get(
            "bing_fallback_enabled", True
        ):
            bing_urls = await self.scraper_mgr.fetch_bing_image_urls(
                keyword, target_count * 3
            )
            bing_pool = bing_urls.copy() if bing_urls else []

            while bing_pool and len(valid_items) < target_count:
                needed = target_count - len(valid_items)
                batch_size = min(len(bing_pool), max(needed, needed * 2))

                batch_urls = bing_pool[:batch_size]
                bing_pool = bing_pool[batch_size:]

                bing_dl = await self.composer_mgr.download_image_batch(
                    batch_urls, target_count=len(batch_urls)
                )
                for url, img_bytes in bing_dl:
                    if len(valid_items) >= target_count:
                        break
                    is_valid, b_hash = await loop.run_in_executor(
                        None, self._validate_and_hash_sync, img_bytes, min_resolution
                    )
                    if is_valid and b_hash not in seen_hashes:
                        valid_items.append((url, img_bytes))
                        seen_hashes.add(b_hash)

            logger.info(f"Bing 补充处理完毕，当前高清有效图片数: {len(valid_items)}")

        return valid_items[:target_count]

    async def _vlm_selection(
        self, event: AstrMessageEvent, items: List[Tuple[str, bytes]], eval_desc: str
    ) -> Tuple[str, bytes, str, bool]:
        collage_bytes, valid_items = await self.composer_mgr.create_collage_from_items(
            items
        )
        if not collage_bytes or not valid_items:
            return "", b"", "图像组合处理失败，候选数据损坏。", False

        vlm_provider = await self._get_vlm_provider(event)
        if vlm_provider:
            async with self._vlm_semaphore:
                best_idx = await select_best_image_index(
                    vlm_provider, collage_bytes, eval_desc, len(valid_items)
                )

            # 🌟 修复魔法数字重载：明确区分拒绝(-1)和异常崩溃(-2)
            if best_idx in (-1, -2):
                if best_idx == -1:
                    logger.info(
                        "VLM 判定候选图均未完美匹配描述，触发软回退，默认下发首张有效候选图。"
                    )
                else:
                    logger.warning(
                        "VLM 调用异常或超出重试限制，触发软回退，默认下发首张有效候选图。"
                    )
                final_url, final_bytes = valid_items[0]
                return final_url, final_bytes, "", True

            final_url, final_bytes = valid_items[best_idx]
            return final_url, final_bytes, "", False
        else:
            logger.info("VLM 模型未配置或获取失败，自动降级为首张有效候选图。")
            return valid_items[0][0], valid_items[0][1], "", True

    def _format_image_sync(self, img_bytes: bytes) -> bytes:
        try:
            with io.BytesIO(img_bytes) as img_io:
                with Image.open(img_io) as img:
                    if img.format not in ["JPEG", "PNG"]:
                        if img.mode in ("RGBA", "LA") or (
                            img.mode == "P" and "transparency" in img.info
                        ):
                            try:
                                img = img.convert("RGBA")
                                bg = Image.new("RGB", img.size, (255, 255, 255))
                                bg.paste(img, mask=img.split()[-1])
                                img = bg
                            except Exception:
                                img = img.convert("RGB")
                        else:
                            img = img.convert("RGB")

                        with io.BytesIO() as buf:
                            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
                            return buf.getvalue()
                    return img_bytes
        except UnidentifiedImageError:
            return img_bytes
        except Exception:
            return img_bytes

    async def _format_image(self, img_bytes: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._format_image_sync, img_bytes)

    async def search(
        self,
        event: AstrMessageEvent,
        keyword: str,
        description: str = "",
        use_vlm_selection: bool = True,
    ) -> Tuple[Optional[bytes], str, bool]:
        eval_desc = description if description else keyword
        items = await self._ensure_minimum_images(keyword)

        if not items:
            return None, "未找到符合分辨率要求且可访问的图像资源。", False

        final_bytes = b""
        is_fallback = False

        if use_vlm_selection and len(items) > 1:
            _, final_bytes, err_msg, is_fallback = await self._vlm_selection(
                event, items, eval_desc
            )
            if not final_bytes:
                return None, err_msg, False
        else:
            _, final_bytes = items[0]

        final_bytes = await self._format_image(final_bytes)
        return final_bytes, "", is_fallback
