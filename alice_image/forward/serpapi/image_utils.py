"""网络与图片工具：HttpService（代理/UA/权限/共享会话）+ 从消息提取图片源的辅助函数。"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.message_components import Image, Reply

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
HTTP_TIMEOUT_SECONDS = 30
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 图床上传端点（均为匿名上传，无需 API Key）
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"  # 永久
LITTERBOX_UPLOAD_URL = (
    "https://litterbox.catbox.moe/resources/internals/api.php"  # 临时(固定 1h)
)
UGUU_UPLOAD_URL = "https://uguu.se/upload.php"  # 临时(约 3h)，响应为 JSON
IMAGE_HOSTS = ("litterbox", "uguu", "catbox")
# Litterbox 文件保留时长：以图搜图只需链接存活几分钟，固定 1h 足够
LITTERBOX_TIME = "1h"
# 各图床单文件大小上限（仅作安全钳制；以图搜图用的图通常只有几 MB）
_HOST_MAX_BYTES = {
    "catbox": 200 * 1024 * 1024,
    "litterbox": 1024 * 1024 * 1024,
    "uguu": 128 * 1024 * 1024,
}
# 共享会话的最大并发连接数：拼图阶段会并发下载大量候选图，显式设上限而非依赖 aiohttp 默认值
HTTP_CONNECTION_LIMIT = 64


def _read_file_bytes(file_path: str) -> bytes:
    """读取本地文件字节数据（供 to_thread 调用）。"""
    with open(file_path, "rb") as f:
        return f.read()


def _guess_image_type(data: bytes) -> tuple[str, str]:
    """据文件头(magic bytes)推断图片 (扩展名, MIME)，未知则回退 jpg。

    图床按上传文件名后缀生成公网 URL；一律按 jpg 会让 PNG/GIF 得到与内容不符的
    .jpg 链接，可能被下游(SerpApi)误判，故按真实类型命名。
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "jpg", "image/jpeg"


class HttpService:
    """封装网络配置（代理 / UA / 权限开关）与共享 aiohttp 会话，由插件实例持有并向下传递。"""

    def __init__(
        self,
        proxy_url: str = "",
        user_agent: str = "",
        allow_image_upload: bool = True,
        image_host: str = "litterbox",
    ) -> None:
        self.proxy_url: str | None = self._normalize_proxy(proxy_url)
        self._user_agent = (
            user_agent.strip() if user_agent and user_agent.strip() else None
        )
        self.allow_image_upload = bool(allow_image_upload)
        self.image_host = image_host if image_host in IMAGE_HOSTS else "litterbox"
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        if self.proxy_url:
            logger.info(f"[alice_image_serpapi] 已设置代理: {self.proxy_url}")

    @staticmethod
    def _normalize_proxy(proxy_url: str | None) -> str | None:
        if not proxy_url or not proxy_url.strip():
            return None
        cleaned = proxy_url.strip()
        if cleaned.startswith(("http://", "https://")):
            return cleaned
        logger.warning(
            f"[alice_image_serpapi] 不支持的代理协议（仅支持 http/https），已忽略该代理配置: {cleaned}"
        )
        return None

    @property
    def user_agent(self) -> str:
        """当前 User-Agent，未配置则用内置默认值。"""
        return self._user_agent or DEFAULT_USER_AGENT

    async def session(self) -> aiohttp.ClientSession:
        """获取共享的 aiohttp ClientSession（代理由各请求通过 proxy 参数控制）。"""
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                connector = aiohttp.TCPConnector(
                    limit=HTTP_CONNECTION_LIMIT, ttl_dns_cache=300
                )
                self._session = aiohttp.ClientSession(connector=connector)
            return self._session

    async def close(self) -> None:
        """关闭共享会话（插件卸载时调用）。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def read_image_bytes(self, source: str) -> bytes | None:
        r"""读取本地/内联图片源（file://、本地路径、base64://、data:image）的字节，作为取图回退路径。"""
        if not source:
            return None

        if (
            source.startswith("file://")
            or re.match(r"^[A-Za-z]:[/\\]", source)
            or source.startswith("/")
        ):
            if source.startswith("file://"):
                file_path = source[7:]
                if (
                    file_path.startswith("/")
                    and len(file_path) > 2
                    and file_path[2] == ":"
                ):
                    file_path = file_path[1:]
            else:
                file_path = source
            try:
                if await asyncio.to_thread(os.path.exists, file_path):
                    return await asyncio.to_thread(_read_file_bytes, file_path)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[alice_image_serpapi] 读取本地文件失败: {e}")
            return None

        if source.startswith("base64://"):
            try:
                return base64.b64decode(source[9:])
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[alice_image_serpapi] base64 解码失败: {e}")
            return None

        if source.startswith("data:image"):
            match = re.match(r"data:image/\w+;base64,(.+)", source)
            if match:
                try:
                    return base64.b64decode(match.group(1))
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"[alice_image_serpapi] data URI 解码失败: {e}")
            return None

        return None

    async def upload_image(self, image_bytes: bytes) -> str | None:
        """把图片上传到所选图床（litterbox/uguu/catbox，均匿名免 Key），返回公网 URL。"""
        if not image_bytes:
            return None
        host = self.image_host
        if len(image_bytes) > _HOST_MAX_BYTES.get(host, 200 * 1024 * 1024):
            logger.warning(f"[alice_image_serpapi] 图片过大，超过 {host} 图床大小上限")
            return None
        try:
            if host == "uguu":
                return await self._upload_uguu(image_bytes)
            if host == "catbox":
                return await self._upload_catbox_family(
                    CATBOX_UPLOAD_URL, {"reqtype": "fileupload"}, image_bytes, "Catbox"
                )
            return await self._upload_catbox_family(
                LITTERBOX_UPLOAD_URL,
                {"reqtype": "fileupload", "time": LITTERBOX_TIME},
                image_bytes,
                "Litterbox",
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"[alice_image_serpapi] 图床上传异常({host}): {e}")
            return None

    def _build_form(
        self, file_field: str, image_bytes: bytes, extra: dict | None = None
    ) -> aiohttp.FormData:
        """构造 multipart 表单：附加字段 + 按真实类型命名的图片文件字段。"""
        ext, content_type = _guess_image_type(image_bytes)
        data = aiohttp.FormData()
        for key, value in (extra or {}).items():
            data.add_field(key, value)
        data.add_field(
            file_field, image_bytes, filename=f"image.{ext}", content_type=content_type
        )
        return data

    async def _upload_catbox_family(
        self, endpoint: str, fields: dict, image_bytes: bytes, name: str
    ) -> str | None:
        """Catbox / Litterbox：fileToUpload 上传，响应体即公网 URL（纯文本）。"""
        client_timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        session = await self.session()
        data = self._build_form("fileToUpload", image_bytes, fields)
        async with session.post(
            endpoint,
            data=data,
            headers={"User-Agent": self.user_agent},
            timeout=client_timeout,
            proxy=self.proxy_url,
        ) as resp:
            text = (await resp.text()).strip()
            if resp.status == 200 and text.startswith("https://"):
                logger.info(f"[alice_image_serpapi] 已上传到 {name} 图床: {text}")
                return text
            logger.warning(
                f"[alice_image_serpapi] {name} 上传失败: HTTP {resp.status}, 响应={text[:200]}"
            )
        return None

    async def _upload_uguu(self, image_bytes: bytes) -> str | None:
        """uguu.se：files[] 上传，响应为 JSON {success, files:[{url}]}。"""
        client_timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
        session = await self.session()
        data = self._build_form("files[]", image_bytes)
        async with session.post(
            UGUU_UPLOAD_URL,
            data=data,
            headers={"User-Agent": self.user_agent},
            timeout=client_timeout,
            proxy=self.proxy_url,
        ) as resp:
            text = (await resp.text()).strip()
            if resp.status == 200:
                try:
                    obj = json.loads(text)
                    if obj.get("success") and obj.get("files"):
                        url = (obj["files"][0] or {}).get("url", "")
                        if url.startswith("https://"):
                            logger.info(
                                f"[alice_image_serpapi] 已上传到 uguu 图床: {url}"
                            )
                            return url
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
                logger.warning(f"[alice_image_serpapi] uguu 返回异常: {text[:200]}")
            else:
                logger.warning(
                    f"[alice_image_serpapi] uguu 上传失败: HTTP {resp.status}"
                )
        return None

    async def get_public_url_for_image(self, image: Image) -> str | None:
        """取图片的公网 URL：已是 http(s) 直接复用，否则用 convert_to_base64() 还原字节上传图床。"""
        if image is None:
            return None

        raw = (image.url or image.file or "").strip()
        if raw.startswith(("http://", "https://")):
            logger.info(
                f"[alice_image_serpapi] 图片已是公网 URL，直接用于检索（未上传图床）: {raw}"
            )
            return raw

        if not self.allow_image_upload:
            logger.warning(
                "[alice_image_serpapi] 图床上传已禁用，仅支持公网 URL 图片。"
                "如需以图搜本地图片，请在配置中开启 allow_image_upload。"
            )
            return None

        # 优先用框架解析器还原字节（兼容预处理落地的本地临时文件）；失败再回退到按源字符串本地读取。
        image_bytes: bytes | None = None
        try:
            b64 = await image.convert_to_base64()
            if b64:
                image_bytes = base64.b64decode(b64)
        except Exception as e:  # noqa: BLE001
            logger.debug(
                f"[alice_image_serpapi] convert_to_base64 还原图片失败，回退本地读取: {e}"
            )
        if not image_bytes:
            image_bytes = await self.read_image_bytes(raw)
        if not image_bytes:
            logger.warning("[alice_image_serpapi] 读取图片字节失败，无法上传图床。")
            return None

        logger.info(
            "[alice_image_serpapi] 图片为本地/内联来源，准备上传到图床以获取公网 URL..."
        )
        return await self.upload_image(image_bytes)


def extract_image_from_event(event: Any) -> Image | None:
    """提取消息中第一张图片组件（优先正文，其次 Reply 链）。

    返回组件本身而非 url 字符串，以便上层用 convert_to_base64() 取字节。
    """
    messages = event.get_messages() or []
    for comp in messages:
        if isinstance(comp, Image) and (comp.url or comp.file):
            return comp
    for comp in messages:
        if isinstance(comp, Reply) and getattr(comp, "chain", None):
            for sub in comp.chain:
                if isinstance(sub, Image) and (sub.url or sub.file):
                    return sub
    return None
