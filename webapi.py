"""爱丽丝的图片助手 · Dashboard WebUI 后端。

本模块分两层：

* AliceWebService —— 纯业务层，只依赖插件实例本身（通过 getattr 兜底取件），
  负责组装元信息、健康检查、配置读写、找图与溯源的一次性调用。它不认识任何
  Web 框架，可以在单测里直接实例化。
* AliceWebApi —— HTTP 适配层，把 Dashboard bridge 的调用映射到上面的业务方法，
  并同时适配 astrbot.api.web 与 Quart 两种运行时。

安全须知（同时写在 WebUI 的「关于」页）：

1. 所有接口都通过 Context.register_web_api 注册，鉴权完全依赖 AstrBot Dashboard
   的登录态，本模块不做二次校验；请勿把 Dashboard 暴露到公网。
2. Pixiv refresh token / SauceNAO Key / SerpApi Key 属于敏感凭据，读接口一律以
   掩码返回，写接口收到掩码原文会直接拒绝，避免误把掩码写回配置。
3. 找图与溯源都会主动出网，并可能把图片交给 VLM 复核；涉密图片不要上传。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import io
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .alice_image.webui.commands import command_catalog, command_count
from .alice_image.webui.schema import (
    MASK,
    apply_changes,
    build_groups,
    count_fields,
    load_schema,
)
from .alice_image.webui.themes import (
    THEMES,
    default_theme_key,
    normalize_theme,
    theme_payload,
)

PLUGIN_ID = "astrbot_plugin_alice_image_assistant"
PLUGIN_DISPLAY_NAME = "爱丽丝的图片助手"
PLUGIN_TAGLINE = "自主找图 · 视觉挑图 · 以图溯源"
PLUGIN_REPO = "https://github.com/Whereis-Alice/astrbot_plugin_alice_image_assistant"
PAGE_NAME = "alice"
STATE_KV_KEY = "webui_state"


def _declared_version() -> str:
    """从同目录 metadata.yaml 读版本号，去掉前缀 v。

    直接读盘而不是从 main 导入，避免循环导入，也保证 WebUI 不会展示过期版本。
    """

    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - 打包异常时的兜底
        return "0.0.0"
    for line in text.splitlines():
        if not line.startswith("version:"):
            continue
        value = line.split(":", 1)[1].strip().strip("\"'")
        if value:
            return value.lstrip("vV")
    return "0.0.0"


PLUGIN_VERSION = _declared_version()

#: 预览图最长边（像素）。超过就缩，避免把整张原图塞进 JSON。
PREVIEW_MAX_SIDE = 640
#: 小于这个字节数的图直接原样内联，省一次重编码。
PREVIEW_PASSTHROUGH_BYTES = 320_000
#: 单次找图允许的最大张数（对齐命令层的体感，不做批量爬取）。
SEARCH_COUNT_MAX = 5
#: 溯源结果里最多生成多少张缩略图。
PREVIEW_MAX_ITEMS = 9
IMAGE_CACHE_TTL_SECONDS = 900.0
IMAGE_CACHE_MAX_ITEMS = 24
IMAGE_CACHE_MAX_BYTES = 48 * 1024 * 1024
UPLOAD_MAX_BYTES = 10 * 1024 * 1024
TOKEN_BYTES = 12
QUERY_MAX_LENGTH = 200

SOURCE_ORDER: tuple[str, ...] = ("soutu", "serpapi", "pixiv")
SOURCE_LABELS: dict[str, str] = {
    "pixiv": "Pixiv",
    "soutu": "搜图神器",
    "serpapi": "SerpApi",
}
STRATEGY_LABELS: dict[str, str] = {
    "saucenao": "SauceNAO",
    "google_lens": "Google Lens",
    "ascii2d": "ascii2d",
}
STATE_TABS: frozenset[str] = frozenset(
    {"overview", "search", "reverse", "config", "commands", "about"}
)

_MIME_BY_FORMAT: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "GIF": "image/gif",
    "WEBP": "image/webp",
    "BMP": "image/bmp",
}
_EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


class AliceWebError(Exception):
    """能安全回给前端的错误：message 面向用户，status_code 决定 HTTP 码。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# 图片缓存
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ImageEntry:
    """缓存里的一张图。token 是前端唯一可见的句柄。"""

    token: str
    data: bytes
    mime: str
    width: int
    height: int
    label: str
    source: str
    filename: str
    created: float


class ImageCache:
    """带 TTL / 条数 / 总字节三重上限的进程内图片缓存。

    WebUI 找图和溯源都会产出原图字节，直接塞进 JSON 会炸；这里只把字节留在后端，
    前端拿 token 去 image 接口取，同时保证插件不会被大图撑爆内存。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = IMAGE_CACHE_TTL_SECONDS,
        max_items: int = IMAGE_CACHE_MAX_ITEMS,
        max_bytes: int = IMAGE_CACHE_MAX_BYTES,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max_items = int(max_items)
        self._max_bytes = int(max_bytes)
        self._items: OrderedDict[str, _ImageEntry] = OrderedDict()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def purge(self, *, now: float | None = None) -> None:
        """清掉过期条目，并把条数 / 字节压回上限内（先淘汰最久未用的）。"""

        stamp = time.monotonic() if now is None else now
        if self._ttl > 0:
            for token in [
                key for key, entry in self._items.items() if stamp - entry.created > self._ttl
            ]:
                self._drop(token)
        while len(self._items) > self._max_items:
            self._drop(next(iter(self._items)))
        while self._bytes > self._max_bytes and self._items:
            self._drop(next(iter(self._items)))

    def _drop(self, token: str) -> None:
        entry = self._items.pop(token, None)
        if entry is not None:
            self._bytes -= len(entry.data)
            if self._bytes < 0:
                self._bytes = 0

    def put(
        self,
        data: bytes,
        *,
        mime: str,
        width: int = 0,
        height: int = 0,
        label: str = "",
        source: str = "",
        filename: str = "",
    ) -> _ImageEntry:
        payload = bytes(data or b"")
        if not payload:
            raise AliceWebError("图片内容是空的")
        token = secrets.token_urlsafe(TOKEN_BYTES)
        suffix = _EXT_BY_MIME.get(mime, ".bin")
        entry = _ImageEntry(
            token=token,
            data=payload,
            mime=mime,
            width=int(width or 0),
            height=int(height or 0),
            label=label,
            source=source,
            filename=filename or f"alice-{token}{suffix}",
            created=time.monotonic(),
        )
        self._items[token] = entry
        self._bytes += len(payload)
        self.purge()
        return entry

    def get(self, token: str) -> _ImageEntry:
        self.purge()
        key = str(token or "")
        entry = self._items.get(key)
        if entry is None:
            raise AliceWebError("图片已过期或不存在，请重新上传或重新找图", status_code=404)
        self._items.move_to_end(key)
        return entry

    def clear(self) -> None:
        """插件卸载时把缓存里的图片字节立刻释放掉。"""

        self._items.clear()
        self._bytes = 0

    def stats(self) -> dict[str, Any]:
        self.purge()
        return {
            "count": len(self._items),
            "bytes": self._bytes,
            "ttl_seconds": self._ttl,
            "max_items": self._max_items,
            "max_bytes": self._max_bytes,
        }


# ---------------------------------------------------------------------------
# 图片工具
# ---------------------------------------------------------------------------


def _guess_mime(data: bytes) -> str:
    """靠魔数猜 MIME，Pillow 不可用或解码失败时兜底。"""

    head = bytes(data[:16])
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    return ""


def probe_image(data: bytes) -> tuple[int, int, str]:
    """返回 (宽, 高, MIME)。Pillow 认不出来时退回魔数，宽高给 0。"""

    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow 是硬依赖，仅防御
        return 0, 0, _guess_mime(data)
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            mime = _MIME_BY_FORMAT.get(str(image.format or "").upper(), _guess_mime(data))
            return int(width), int(height), mime or "application/octet-stream"
    except Exception:
        return 0, 0, _guess_mime(data)


def make_preview(data: bytes, *, max_side: int = PREVIEW_MAX_SIDE) -> str:
    """生成可直接塞进 img src 的 data URL。

    小图原字节直出（保住 GIF 动图和透明通道）；大图取首帧缩到 max_side 后转
    JPEG，质量 82，兼顾清晰度与 JSON 体积。
    """

    payload = bytes(data or b"")
    if not payload:
        return ""
    mime = _guess_mime(payload) or "application/octet-stream"
    if len(payload) <= PREVIEW_PASSTHROUGH_BYTES:
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as image:
            with contextlib.suppress(Exception):
                image.seek(0)
            frame = image.convert("RGB")
            frame.thumbnail((max_side, max_side), Image.LANCZOS)
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=82, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        encoded = base64.b64encode(payload).decode("ascii")
        return f"data:{mime};base64,{encoded}"


# ---------------------------------------------------------------------------
# 假事件
# ---------------------------------------------------------------------------


class WebUiEvent:
    """喂给后端搜索服务的最小事件替身。

    后端只会读 unified_msg_origin、平台名，并可能 await event.send(...)。这里把
    发出去的消息收集到 sent 里，WebUI 再从中把图片字节抠出来。

    刻意不写 __getattr__ 兜底：否则 getattr(event, "任意属性", None) 全部为真，
    会让后端走进依赖真实聊天上下文的分支。
    """

    def __init__(self, *, origin: str = "") -> None:
        self.unified_msg_origin = origin
        self.sent: list[Any] = []

    def get_platform_name(self) -> str:
        return "webui"

    def get_sender_id(self) -> str:
        return "webui"

    def get_sender_name(self) -> str:
        return "Dashboard"

    def get_self_id(self) -> str:
        return "webui"

    def get_group_id(self) -> str:
        return ""

    def get_message_outline(self) -> str:
        return ""

    def is_private_chat(self) -> bool:
        return True

    async def send(self, message: Any) -> None:
        self.sent.append(message)

    def plain_result(self, text: str) -> str:
        return text

    def chain_result(self, chain: Any) -> Any:
        return chain

    def stop_event(self) -> None:
        return None


def _iter_components(message: Any, depth: int = 0) -> list[Any]:
    """把消息 / 消息链摊平成组件列表，最多下钻 4 层。"""

    if message is None or depth > 4:
        return []
    chain = getattr(message, "chain", None)
    if isinstance(chain, Sequence) and not isinstance(chain, (str, bytes)):
        out: list[Any] = []
        for item in chain:
            out.extend(_iter_components(item, depth + 1))
        return out
    if isinstance(message, Mapping):
        return [message]
    if isinstance(message, Sequence) and not isinstance(message, (str, bytes)):
        out = []
        for item in message:
            out.extend(_iter_components(item, depth + 1))
        return out
    return [message]


def collect_image_sources(messages: Sequence[Any]) -> list[str]:
    """从 WebUiEvent.sent 里抽出图片来源（URL / 本地路径 / base64）。"""

    found: list[str] = []
    seen: set[str] = set()
    for message in messages:
        for component in _iter_components(message):
            if isinstance(component, Mapping):
                if str(component.get("type") or "").lower() != "image":
                    continue
                candidates = [
                    component.get("file"),
                    component.get("url"),
                    component.get("path"),
                ]
            else:
                if type(component).__name__ != "Image":
                    continue
                candidates = [
                    getattr(component, "file", None),
                    getattr(component, "url", None),
                    getattr(component, "path", None),
                ]
            for candidate in candidates:
                text = str(candidate or "").strip()
                if text and text not in seen:
                    seen.add(text)
                    found.append(text)
                    break
    return found


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把配置里的整数值夹进 [minimum, maximum]，非法值一律退回 default。"""

    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default
    return max(minimum, min(maximum, number))


def _strategy_key(name: str) -> str:
    """把策略展示名归一成稳定 key：Google Lens -> google_lens。"""

    return str(name or "").strip().lower().replace(" ", "_").replace("-", "_")


def _strategy_label(key: str, fallback: str = "") -> str:
    return STRATEGY_LABELS.get(key, fallback or key)


# ---------------------------------------------------------------------------
# 业务层
# ---------------------------------------------------------------------------


class AliceWebService:
    """WebUI 的业务门面。

    构造只需要插件实例；所有取件都走 getattr 兜底，这样即使插件某个子系统没
    初始化成功（例如 Pixiv 未配置 token），WebUI 依然能打开并给出诊断信息。
    """

    def __init__(self, plugin: Any, *, plugin_dir: Path | None = None) -> None:
        self._plugin = plugin
        self._plugin_dir = Path(plugin_dir or Path(__file__).resolve().parent)
        self._schema_cache: dict[str, Any] | None = None
        webui = self._sub(getattr(plugin, "raw_config", None), "webui")
        self._cache_ttl = _clamp_int(
            webui.get("preview_ttl_seconds"),
            int(IMAGE_CACHE_TTL_SECONDS),
            60,
            7200,
        )
        self._cache_max_items = _clamp_int(
            webui.get("preview_cache_max_items"),
            IMAGE_CACHE_MAX_ITEMS,
            4,
            128,
        )
        self._images = ImageCache(
            ttl_seconds=self._cache_ttl,
            max_items=self._cache_max_items,
        )
        self._provider_lock = asyncio.Lock()

    # -- 取件 -------------------------------------------------------------
    @property
    def plugin(self) -> Any:
        return self._plugin

    @property
    def images(self) -> ImageCache:
        return self._images

    def _context(self) -> Any:
        return getattr(self._plugin, "context", None)

    def _config_root(self) -> Any:
        return getattr(self._plugin, "raw_config", None)

    def _find_config(self) -> Mapping[str, Any]:
        value = getattr(self._plugin, "find_config", None)
        return value if isinstance(value, Mapping) else {}

    def _reverse_config(self) -> Mapping[str, Any]:
        value = getattr(self._plugin, "reverse_config", None)
        return value if isinstance(value, Mapping) else {}

    def _orchestrator(self) -> Any:
        return getattr(self._plugin, "forward", None)

    def _reverse_service(self) -> Any:
        controller = getattr(self._plugin, "reverse", None)
        return getattr(controller, "service", None)

    @staticmethod
    def _sub(root: Any, *keys: str) -> Mapping[str, Any]:
        node: Any = root
        for key in keys:
            if not isinstance(node, Mapping):
                return {}
            node = node.get(key)
        return node if isinstance(node, Mapping) else {}

    def schema(self) -> dict[str, Any]:
        if self._schema_cache is None:
            self._schema_cache = load_schema(self._plugin_dir)
        return self._schema_cache

    # -- 元信息 -----------------------------------------------------------
    def _sources(self) -> list[dict[str, Any]]:
        orchestrator = self._orchestrator()
        out: list[dict[str, Any]] = []
        for key in SOURCE_ORDER:
            enabled = False
            probe = getattr(orchestrator, "_available", None)
            if callable(probe):
                with contextlib.suppress(Exception):
                    enabled = bool(probe(key))
            out.append({"key": key, "label": SOURCE_LABELS.get(key, key), "enabled": enabled})
        return out

    def _strategies(self) -> list[dict[str, Any]]:
        service = self._reverse_service()
        names: list[str] = []
        probe = getattr(service, "get_available_strategies", None)
        if callable(probe):
            with contextlib.suppress(Exception):
                names = [str(name) for name in probe() or []]
        active = {_strategy_key(name) for name in names}
        out: list[dict[str, Any]] = []
        for key, label in STRATEGY_LABELS.items():
            out.append({"key": key, "label": label, "enabled": key in active})
        for name in names:
            key = _strategy_key(name)
            if key not in STRATEGY_LABELS:
                out.append({"key": key, "label": name, "enabled": True})
        return out

    def meta(self) -> dict[str, Any]:
        """页面首屏需要的一切静态信息（不出网、不阻塞）。"""

        find = self._find_config()
        reverse = self._reverse_config()
        groups = build_groups(self.schema(), self._config_root())
        sources = self._sources()
        strategies = self._strategies()
        return {
            "status": "ok",
            "plugin": {
                "name": PLUGIN_ID,
                "display_name": PLUGIN_DISPLAY_NAME,
                "version": PLUGIN_VERSION,
                "repo": PLUGIN_REPO,
                "author": "Whereis-Alice",
                "astrbot_version": ">=4.16,<5",
                "tagline": PLUGIN_TAGLINE,
            },
            "page": PAGE_NAME,
            "themes": theme_payload(),
            "default_theme": default_theme_key(),
            "features": {
                "find_image": bool(find.get("enabled", True)),
                "reverse_image": bool(reverse.get("enabled", True)),
                "pixiv": bool(self._sub(find, "pixiv").get("enabled", True)),
                "soutu": bool(self._sub(find, "soutu").get("enabled", True)),
                "serpapi": bool(self._sub(find, "serpapi").get("enabled", True)),
                "llm_tools": bool(find.get("llm_tools_enabled", True))
                or bool(reverse.get("llm_tools_enabled", True)),
                "llm_review": bool(self._sub(find, "llm_review").get("enabled", True)),
            },
            "sources": sources,
            "strategies": strategies,
            "counts": {
                "commands": command_count(),
                "config_fields": count_fields(groups),
                "themes": len(THEMES),
                "sources": sum(1 for item in sources if item["enabled"]),
                "strategies": sum(1 for item in strategies if item["enabled"]),
            },
            "limits": {
                "search_count_max": SEARCH_COUNT_MAX,
                "preview_max_items": PREVIEW_MAX_ITEMS,
                "preview_ttl_seconds": self._cache_ttl,
                "upload_max_bytes": UPLOAD_MAX_BYTES,
                "query_max_length": QUERY_MAX_LENGTH,
            },
        }

    # -- 健康检查 ---------------------------------------------------------
    @staticmethod
    def _check(
        key: str,
        label: str,
        level: str,
        value: str,
        hint: str = "",
    ) -> dict[str, Any]:
        return {
            "key": key,
            "label": label,
            "level": level,
            "value": value,
            "hint": hint,
        }

    def _image_context_stats(self) -> dict[str, int]:
        """统计以图搜图的会话图片上下文占用（失败就返回 0，不影响页面）。"""

        try:
            from .alice_image.reverse.image_context import get_image_context_manager

            manager = get_image_context_manager()
        except Exception:
            return {"sessions": 0, "images": 0}
        sessions = getattr(manager, "sessions", None)
        if not isinstance(sessions, Mapping):
            sessions = getattr(manager, "_sessions", None)
        if not isinstance(sessions, Mapping):
            return {"sessions": 0, "images": 0}
        images = 0
        for session in sessions.values():
            reader = getattr(session, "get_all_image_infos", None)
            if not callable(reader):
                continue
            with contextlib.suppress(Exception):
                images += len(reader() or [])
        return {"sessions": len(sessions), "images": images}

    @staticmethod
    def _probe_playwright_sync() -> tuple[bool, str]:
        try:
            import playwright  # noqa: F401
            from playwright.async_api import async_playwright  # noqa: F401
        except Exception as error:
            return False, str(error)
        return True, ""

    async def _offload_probe_playwright(self) -> tuple[bool, str]:
        return await asyncio.to_thread(self._probe_playwright_sync)

    @staticmethod
    def _provider_id(provider: Any) -> str:
        if provider is None:
            return ""
        meta = getattr(provider, "meta", None)
        if callable(meta):
            with contextlib.suppress(Exception):
                return str(getattr(meta(), "id", "") or "")
        return str(getattr(provider, "id", "") or "")

    def _describe_provider(self, provider: Any) -> dict[str, str]:
        if provider is None:
            return {"id": "", "model": ""}
        model = ""
        getter = getattr(provider, "get_model", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                model = str(getter() or "")
        return {"id": self._provider_id(provider), "model": model}

    async def _resolve_webui_provider_id(self) -> str:
        """WebUI 没有会话上下文，这里挑一个「当下能用」的 provider id。

        先问 Context 当前在用的，再退回第一个已注册的。拿不到就返回空串，调用方
        据此提示用户去配置里显式指定 provider。
        """

        context = self._context()
        if context is None:
            return ""
        getter = getattr(context, "get_using_provider", None)
        if callable(getter):
            try:
                provider = getter()
                if inspect.isawaitable(provider):
                    provider = await provider
                found = self._provider_id(provider)
                if found:
                    return found
            except Exception:
                pass
        lister = getattr(context, "get_all_providers", None)
        if callable(lister):
            with contextlib.suppress(Exception):
                providers = list(lister() or [])
                if providers:
                    return self._provider_id(providers[0])
        return ""

    async def health(self) -> dict[str, Any]:
        """把「能不能用」摊开成一张可读的清单，替代翻日志。"""

        find = self._find_config()
        reverse = self._reverse_config()
        pixiv_settings = self._sub(find, "pixiv", "settings")
        soutu = self._sub(find, "soutu")
        serpapi = self._sub(find, "serpapi")
        review = self._sub(find, "llm_review")
        api_keys = self._sub(reverse, "api_keys")

        checks: list[dict[str, Any]] = []
        checks.append(
            self._check(
                "web_backend",
                "Web 运行时",
                "ok" if _WEB_BACKEND == "astrbot" else "warn",
                _WEB_BACKEND,
                ""
                if _WEB_BACKEND == "astrbot"
                else "当前退回兼容后端，建议升级到 AstrBot 4.16 及以上",
            )
        )
        checks.append(
            self._check(
                "find_image",
                "找图总开关",
                "ok" if find.get("enabled", True) else "warn",
                "已开启" if find.get("enabled", True) else "已关闭",
                "" if find.get("enabled", True) else "开启后才会响应找图指令与 LLM 工具",
            )
        )
        checks.append(
            self._check(
                "reverse_image",
                "溯源总开关",
                "ok" if reverse.get("enabled", True) else "warn",
                "已开启" if reverse.get("enabled", True) else "已关闭",
                "" if reverse.get("enabled", True) else "开启后才能以图搜图",
            )
        )
        token = str(pixiv_settings.get("refresh_token") or "").strip()
        checks.append(
            self._check(
                "pixiv_token",
                "Pixiv refresh token",
                "ok" if token else "warn",
                "已填写" if token else "未填写",
                "" if token else "缺少 token 时 Pixiv 相关指令全部不可用",
            )
        )
        sauce = str(api_keys.get("saucenao_api_key") or "").strip()
        checks.append(
            self._check(
                "saucenao",
                "SauceNAO API Key",
                "ok" if sauce else "warn",
                "已填写" if sauce else "未填写",
                "" if sauce else "SauceNAO 策略会直接跳过",
            )
        )
        reverse_keys = api_keys.get("serpapi_keys")
        reverse_key_count = (
            len(reverse_keys)
            if isinstance(reverse_keys, Sequence) and not isinstance(reverse_keys, (str, bytes))
            else 0
        )
        forward_keys = serpapi.get("serpapi_keys")
        forward_key_count = (
            len(forward_keys)
            if isinstance(forward_keys, Sequence) and not isinstance(forward_keys, (str, bytes))
            else 0
        )
        total_keys = reverse_key_count + forward_key_count
        checks.append(
            self._check(
                "serpapi_keys",
                "SerpApi Key",
                "ok" if total_keys else "warn",
                f"溯源 {reverse_key_count} 个 / 找图 {forward_key_count} 个",
                "" if total_keys else "Google Lens 与 SerpApi 找图都需要 Key",
            )
        )
        playwright_ok, playwright_error = await self._offload_probe_playwright()
        checks.append(
            self._check(
                "playwright",
                "Playwright 浏览器内核",
                "ok" if playwright_ok else "warn",
                "可用" if playwright_ok else "不可用",
                ""
                if playwright_ok
                else "搜图神器抓取依赖它；先 pip install playwright，再执行 playwright install chromium"
                + (f"（{playwright_error}）" if playwright_error else ""),
            )
        )
        provider_id = await self._resolve_webui_provider_id()
        configured = (
            str(review.get("provider_id") or "").strip()
            or str(soutu.get("vlm_provider_id") or "").strip()
            or str(serpapi.get("vlm_provider_id") or "").strip()
        )
        checks.append(
            self._check(
                "vlm_provider",
                "视觉复核模型",
                "ok" if (configured or provider_id) else "error",
                configured or provider_id or "未找到",
                ""
                if configured
                else "配置里没显式指定 provider；WebUI 会临时借用当前会话模型，命令与 LLM 工具仍按会话模型走",
            )
        )
        min_resolution = soutu.get("min_resolution", 500)
        try:
            min_resolution_value = int(min_resolution)
        except (TypeError, ValueError):
            min_resolution_value = 500
        checks.append(
            self._check(
                "soutu_min_resolution",
                "搜图神器最小分辨率",
                "ok" if min_resolution_value <= 1200 else "warn",
                f"{min_resolution_value} px",
                ""
                if min_resolution_value <= 1200
                else "阈值偏高会大量丢弃候选图，找图容易空手而归",
            )
        )
        context_stats = self._image_context_stats()
        checks.append(
            self._check(
                "image_context",
                "图片上下文",
                "ok",
                f"{context_stats['sessions']} 个会话 / {context_stats['images']} 张图",
                "会话里发过的图会被记住，方便直接说「找找刚才那张图的出处」",
            )
        )

        providers: list[dict[str, str]] = []
        context = self._context()
        lister = getattr(context, "get_all_providers", None) if context else None
        if callable(lister):
            with contextlib.suppress(Exception):
                providers = [self._describe_provider(provider) for provider in lister() or []]
        return {
            "status": "ok",
            "checks": checks,
            "runtime": {
                "web_backend": _WEB_BACKEND,
                "playwright": playwright_ok,
                "vlm_provider": configured or provider_id,
                "providers": providers,
                "image_context_sessions": context_stats["sessions"],
                "image_context_images": context_stats["images"],
                "cached_images": self._images.stats(),
            },
        }

    # -- 配置 -------------------------------------------------------------
    def config_payload(self) -> dict[str, Any]:
        groups = build_groups(self.schema(), self._config_root())
        return {
            "status": "ok",
            "groups": groups,
            "counts": {"fields": count_fields(groups)},
            "mask": MASK,
            "requires_reload": True,
        }

    def apply_config(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """按路径写回配置。只有真的改动了才落盘，掩码原文一律拒绝。"""

        root = self._config_root()
        if not isinstance(root, dict):
            raise AliceWebError("插件配置对象不可写，请检查 AstrBot 版本", status_code=500)
        if not isinstance(changes, Mapping) or not changes:
            raise AliceWebError("没有需要保存的改动")
        applied, rejected = apply_changes(self.schema(), root, changes)
        if applied:
            saver = getattr(root, "save_config", None)
            if not callable(saver):
                raise AliceWebError("当前配置对象不支持保存", status_code=500)
            try:
                saver()
            except Exception as error:  # pragma: no cover - 依赖运行时
                raise AliceWebError(f"保存失败：{error}", status_code=500) from error
        payload = self.config_payload()
        payload["applied"] = applied
        payload["rejected"] = rejected
        payload["requires_reload"] = bool(applied)
        return payload

    # -- 指令表 -----------------------------------------------------------
    def commands(self) -> dict[str, Any]:
        groups = command_catalog()
        return {
            "status": "ok",
            "groups": groups,
            "counts": {"commands": command_count(), "groups": len(groups)},
        }

    # -- 界面偏好 ---------------------------------------------------------
    def _normalize_state(self, raw: Any) -> dict[str, Any]:
        data = raw if isinstance(raw, Mapping) else {}
        tab = str(data.get("tab") or "overview")
        return {
            "theme": normalize_theme(data.get("theme")),
            "tab": tab if tab in STATE_TABS else "overview",
            "compact": bool(data.get("compact", False)),
            "config_group": str(data.get("config_group") or ""),
        }

    async def load_state(self) -> dict[str, Any]:
        reader = getattr(self._plugin, "get_kv_data", None)
        raw: Any = None
        if callable(reader):
            with contextlib.suppress(Exception):
                raw = await reader(STATE_KV_KEY, None)
        return {"status": "ok", "state": self._normalize_state(raw)}

    async def save_state(self, payload: Any) -> dict[str, Any]:
        state = self._normalize_state(payload)
        writer = getattr(self._plugin, "put_kv_data", None)
        saved = False
        if callable(writer):
            try:
                await writer(STATE_KV_KEY, dict(state))
                saved = True
            except Exception:
                saved = False
        return {"status": "ok", "state": state, "persisted": saved}

    # -- 上传与取图 -------------------------------------------------------
    async def adopt_upload(self, data: bytes, filename: str = "") -> dict[str, Any]:
        payload = bytes(data or b"")
        if not payload:
            raise AliceWebError("没有收到图片内容")
        if len(payload) > UPLOAD_MAX_BYTES:
            raise AliceWebError(
                f"图片太大了（上限 {UPLOAD_MAX_BYTES // (1024 * 1024)} MB）",
                status_code=413,
            )
        width, height, mime = await asyncio.to_thread(probe_image, payload)
        if not mime or not mime.startswith("image/"):
            raise AliceWebError("这不像是一张能识别的图片")
        entry = self._images.put(
            payload,
            mime=mime,
            width=width,
            height=height,
            label="手动上传",
            source="upload",
            filename=str(filename or "").strip(),
        )
        preview = await asyncio.to_thread(make_preview, payload)
        return {
            "status": "ok",
            "token": entry.token,
            "preview": preview,
            "width": entry.width,
            "height": entry.height,
            "bytes": len(payload),
            "mime": entry.mime,
            "filename": entry.filename,
        }

    def image(self, token: str) -> tuple[bytes, str, str]:
        entry = self._images.get(token)
        return entry.data, entry.mime, entry.filename

    @staticmethod
    def _clamp_count(value: Any) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, min(SEARCH_COUNT_MAX, number))

    # -- provider 临时接管 -------------------------------------------------
    @contextlib.asynccontextmanager
    async def _patched_provider(self, kind: str, service: Any) -> Any:
        """WebUI 没有会话，视觉复核会拿不到 provider，这里临时借一个。

        SessionReviewResolver 的逻辑是：配置里显式写了 provider_id 就用它，否则
        按 event.unified_msg_origin 找当前会话模型。WebUI 的假事件没有 origin，
        于是解析结果为 None，复核会被静默跳过。所以这里在调用期间把 provider_id
        写进对应槽位，结束后无条件恢复原值。若用户本来就配了，保持原样不动。
        """

        slots = {
            "soutu": ("config", "vlm_provider_id"),
            "serpapi": ("attr", "vlm_provider_id"),
            "pixiv": ("review_config", "provider_id"),
        }
        target = slots.get(kind)
        if service is None or target is None:
            yield ""
            return
        holder, field = target
        if holder == "attr":
            container: Any = service
            current = str(getattr(service, field, "") or "").strip()
        else:
            container = getattr(service, holder, None)
            if not isinstance(container, dict):
                yield ""
                return
            current = str(container.get(field) or "").strip()
        if current:
            yield current
            return
        borrowed = await self._resolve_webui_provider_id()
        if not borrowed:
            yield ""
            return
        async with self._provider_lock:
            try:
                if holder == "attr":
                    setattr(container, field, borrowed)
                else:
                    container[field] = borrowed
                yield borrowed
            finally:
                if holder == "attr":
                    setattr(container, field, current)
                else:
                    container[field] = current

    # -- 找图 -------------------------------------------------------------
    def _adopt_result_bytes(
        self,
        data: bytes,
        *,
        source: str,
        label: str = "",
        url: str = "",
        score: float | None = None,
    ) -> dict[str, Any]:
        payload = bytes(data or b"")
        if not payload:
            raise AliceWebError("拿到的图片是空的")
        width, height, mime = probe_image(payload)
        entry = self._images.put(
            payload,
            mime=mime or "application/octet-stream",
            width=width,
            height=height,
            label=label or SOURCE_LABELS.get(source, source),
            source=source,
        )
        return {
            "token": entry.token,
            "url": url,
            "width": entry.width,
            "height": entry.height,
            "bytes": len(payload),
            "preview": make_preview(payload),
            "label": entry.label,
            "source": source,
            "score": score,
        }

    @staticmethod
    def _trace(
        stage: str,
        label: str,
        detail: str = "",
        level: str = "info",
    ) -> dict[str, Any]:
        return {"stage": stage, "label": label, "detail": detail, "level": level}

    async def _search_soutu(
        self,
        query: str,
        description: str,
        *,
        review: bool,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        service = getattr(self._orchestrator(), "soutu", None)
        if service is None:
            return [], "搜图神器未启用", {}
        strict = bool(
            self._sub(self._find_config(), "llm_review").get("strict_match_enabled", True)
        )
        async with self._patched_provider("soutu", service) as provider_id:
            result = await service.search(
                WebUiEvent(),
                query,
                description=description,
                use_vlm_selection=review,
                strict_match_enabled=strict,
            )
        info = {
            "review_fallback": bool(getattr(result, "review_fallback", False)),
            "review_status": getattr(result, "review_status", ""),
            "reviewed_count": getattr(result, "reviewed_count", 0),
            "provider_id": provider_id,
        }
        data = getattr(result, "image_bytes", None)
        if not data:
            return [], str(getattr(result, "error", "") or "没有找到合适的图"), info
        image = self._adopt_result_bytes(
            data,
            source="soutu",
            url=str(getattr(result, "image_url", "") or ""),
        )
        return [image], "", info

    async def _search_serpapi(
        self,
        query: str,
        description: str,
        *,
        review: bool,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        service = getattr(self._orchestrator(), "serpapi", None)
        if service is None:
            return [], "SerpApi 未启用", {}
        strict = bool(
            self._sub(self._find_config(), "llm_review").get("strict_match_enabled", True)
        )
        async with self._patched_provider("serpapi", service) as provider_id:
            result = await service.search(
                WebUiEvent(),
                query,
                description=description,
                review_enabled=review,
                strict_match_enabled=strict,
            )
        info = {
            "review_fallback": bool(getattr(result, "review_fallback", False)),
            "review_status": getattr(result, "review_status", ""),
            "provider_id": provider_id,
        }
        data = getattr(result, "image_bytes", None)
        if not data:
            return [], str(getattr(result, "error", "") or "没有找到合适的图"), info
        image = self._adopt_result_bytes(
            data,
            source="serpapi",
            url=str(getattr(result, "image_url", "") or ""),
        )
        return [image], "", info

    async def _search_pixiv(
        self,
        query: str,
        description: str,
        *,
        review: bool,
        count: int,
    ) -> tuple[list[dict[str, Any]], str, dict[str, Any]]:
        service = getattr(self._orchestrator(), "pixiv", None)
        if service is None:
            return [], "Pixiv 未启用", {}
        event = WebUiEvent()
        async with self._patched_provider("pixiv", service) as provider_id:
            result = await service.search(
                event,
                query,
                description,
                count=count,
                review_enabled=review,
                send_images=True,
                send_wait_timeout_seconds=0,
            )
        ids = [str(item) for item in (getattr(result, "ids", None) or [])]
        info = {
            "review_fallback": bool(getattr(result, "review_fallback", False)),
            "review_status": getattr(result, "review_status", ""),
            "delivery_uncertain": bool(getattr(result, "delivery_uncertain", False)),
            "ids": ids,
            "provider_id": provider_id,
        }
        if not getattr(result, "success", False):
            return [], str(getattr(result, "error", "") or "没有找到合适的作品"), info

        from .alice_image.reverse.utils import read_image_bytes

        images: list[dict[str, Any]] = []
        for source in collect_image_sources(event.sent):
            if len(images) >= count:
                break
            try:
                data = await read_image_bytes(source)
            except Exception:
                data = None
            if not data:
                continue
            with contextlib.suppress(AliceWebError):
                images.append(
                    self._adopt_result_bytes(
                        data,
                        source="pixiv",
                        url=source if source.startswith("http") else "",
                    )
                )
        if images:
            return images, "", info
        if ids:
            links = "、".join(f"https://www.pixiv.net/artworks/{pid}" for pid in ids[:count])
            info["hint"] = (
                "Pixiv 命中了作品但没能把图片字节交给 WebUI（当前 image_send_method 可能是 url）。"
                "把 find_image.pixiv.settings.image_send_method 改成 byte 或 file 后即可在此直接预览。"
            )
            return [], f"已找到作品，可直接在 Pixiv 打开：{links}", info
        return [], "Pixiv 没有返回可用的图片", info

    async def search(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """一次性找图：按来源顺序试，命中即停，全程回传可读轨迹。"""

        query = str(payload.get("query") or "").strip()
        if not query:
            raise AliceWebError("先写点想找的东西")
        if len(query) > QUERY_MAX_LENGTH:
            raise AliceWebError(f"关键词太长了（上限 {QUERY_MAX_LENGTH} 字）")
        requested = str(payload.get("source") or "auto").strip().lower()
        if requested not in {"auto", *SOURCE_ORDER}:
            raise AliceWebError("来源只能是 auto / soutu / serpapi / pixiv")
        count = self._clamp_count(payload.get("count", 1))
        review = bool(payload.get("review", True))
        description = str(payload.get("description") or "").strip() or query

        orchestrator = self._orchestrator()
        if orchestrator is None:
            raise AliceWebError("找图子系统没有初始化成功，请查看 AstrBot 日志", status_code=503)
        chooser = getattr(orchestrator, "choose_sources", None)
        order: list[str] = []
        if callable(chooser):
            with contextlib.suppress(Exception):
                order = [str(item) for item in chooser(query, requested) or []]
        if not order:
            raise AliceWebError("没有任何可用的图源，请先在配置页开启并填好凭据", status_code=503)

        started = time.monotonic()
        trace: list[dict[str, Any]] = [
            self._trace(
                "plan",
                "已规划来源顺序",
                " → ".join(SOURCE_LABELS.get(item, item) for item in order),
            )
        ]
        if review:
            trace.append(self._trace("review", "视觉复核", "开启，命中前会让模型逐张确认"))
        errors: list[str] = []
        warnings: list[str] = []
        images: list[dict[str, Any]] = []
        hit_source = ""
        review_fallback = False
        delivery_uncertain = False

        for source in order:
            label = SOURCE_LABELS.get(source, source)
            trace.append(self._trace("attempt", f"尝试 {label}"))
            try:
                if source == "soutu":
                    found, error, info = await self._search_soutu(query, description, review=review)
                elif source == "serpapi":
                    found, error, info = await self._search_serpapi(
                        query, description, review=review
                    )
                else:
                    found, error, info = await self._search_pixiv(
                        query, description, review=review, count=count
                    )
            except AliceWebError:
                raise
            except Exception as error_obj:  # pragma: no cover - 依赖外部服务
                found, error, info = [], f"{type(error_obj).__name__}: {error_obj}", {}
            if info.get("review_fallback"):
                review_fallback = True
                warnings.append(f"{label}：视觉复核未生效，已按原始排序兜底")
            if info.get("delivery_uncertain"):
                delivery_uncertain = True
            if info.get("hint"):
                warnings.append(str(info["hint"]))
            if found:
                images = found
                hit_source = source
                trace.append(self._trace("hit", f"{label} 命中", f"{len(found)} 张", "ok"))
                break
            message = error or "没有结果"
            errors.append(f"{label}：{message}")
            trace.append(self._trace("miss", f"{label} 未命中", message, "warn"))

        elapsed = int((time.monotonic() - started) * 1000)
        return {
            "status": "ok",
            "result": {
                "success": bool(images),
                "source": hit_source,
                "source_label": SOURCE_LABELS.get(hit_source, hit_source),
                "attempted_sources": order,
                "errors": errors,
                "warnings": warnings,
                "review_fallback": review_fallback,
                "delivery_uncertain": delivery_uncertain,
                "count": len(images),
            },
            "images": images,
            "trace": trace,
            "elapsed_ms": elapsed,
        }

    # -- 溯源 -------------------------------------------------------------
    async def _publish_upload(self, entry: _ImageEntry) -> str:
        """把本地字节换成公网可访问的 URL（所有搜图引擎都只吃 URL）。"""

        from .alice_image.reverse.utils import is_image_upload_allowed, upload_image

        if not is_image_upload_allowed():
            raise AliceWebError(
                "当前禁止了图片上传，无法把本地图片交给搜图引擎；"
                "请改用图片链接，或在配置页开启 reverse_image.network.allow_image_upload"
            )
        url = await upload_image(entry.data)
        if not url:
            raise AliceWebError("图片中转上传失败，换用图片链接再试", status_code=502)
        return str(url)

    async def reverse(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """以图搜图：接受图片链接或已上传的 token，返回融合排序后的结果。"""

        service = self._reverse_service()
        if service is None:
            raise AliceWebError("溯源子系统没有初始化成功，请查看 AstrBot 日志", status_code=503)

        raw_strategies = payload.get("strategies")
        names: list[str] = []
        if isinstance(raw_strategies, Sequence) and not isinstance(raw_strategies, (str, bytes)):
            names = [str(item).strip() for item in raw_strategies if str(item).strip()]
        elif isinstance(raw_strategies, str) and raw_strategies.strip():
            names = [raw_strategies.strip()]
        not_found: list[str] = []
        resolver = getattr(service, "resolve_strategy_names", None)
        if names and callable(resolver):
            _, not_found = resolver(names)
            if not_found and len(not_found) == len(names):
                raise AliceWebError(f"没有可用的搜图引擎：{'、'.join(not_found)}")

        image_url = str(payload.get("image_url") or "").strip()
        token = str(payload.get("token") or "").strip()
        if not image_url and not token:
            raise AliceWebError("先上传一张图，或者填一个图片链接")
        if not image_url:
            image_url = await self._publish_upload(self._images.get(token))
        if not image_url.startswith(("http://", "https://")):
            raise AliceWebError("图片链接必须是 http 或 https 开头")

        started = time.monotonic()
        try:
            outcome = await service.explore(image_url, strategy_names=names or None)
        except AliceWebError:
            raise
        except Exception as error:  # pragma: no cover - 依赖外部服务
            raise AliceWebError(
                f"溯源失败：{type(error).__name__}: {error}", status_code=502
            ) from error

        items = list(getattr(outcome, "items", None) or [])
        results: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            thumbnail = ""
            data = getattr(item, "thumbnail_bytes", None)
            if data and index < PREVIEW_MAX_ITEMS:
                with contextlib.suppress(Exception):
                    thumbnail = await asyncio.to_thread(make_preview, data, max_side=240)
            matched = getattr(item, "matched_by", None)
            results.append(
                {
                    "rank": index + 1,
                    "source": str(getattr(item, "source", "") or ""),
                    "source_key": str(getattr(item, "source_key", "") or ""),
                    "title": str(getattr(item, "title", "") or ""),
                    "url": str(getattr(item, "url", "") or ""),
                    "domain": str(getattr(item, "domain", "") or ""),
                    "description": str(getattr(item, "description", "") or ""),
                    "similarity": getattr(item, "similarity", None),
                    "score": getattr(item, "score", None),
                    "matched_by": [str(name) for name in (matched or [])],
                    "thumbnail": thumbnail,
                    "thumbnail_url": str(getattr(item, "thumbnail", "") or ""),
                }
            )

        errors = [str(text) for text in (getattr(outcome, "errors", None) or [])]
        if not_found:
            errors.append(f"忽略了无法识别的引擎：{'、'.join(not_found)}")
        return {
            "status": "ok",
            "image_url": image_url,
            "results": results,
            "errors": errors,
            "counts": {"results": len(results)},
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }


# ---------------------------------------------------------------------------
# HTTP 适配层
# ---------------------------------------------------------------------------


def _detect_web_backend() -> tuple[str, Any]:
    """挑一个可用的 Web 运行时。

    AstrBot >= 4.16 提供框架无关的 astrbot.api.web；旧版本只有 Quart 的请求上下文，
    因此保留 quart 回退；两者都没有时返回 none，插件侧会跳过挂载而不是崩溃。
    """

    try:
        from astrbot.api import web as astrbot_web
    except Exception:  # pragma: no cover - 可选依赖
        astrbot_web = None
    if astrbot_web is not None and hasattr(astrbot_web, "stream_response"):
        return "astrbot", astrbot_web
    try:
        import quart
    except Exception:  # pragma: no cover - 可选依赖
        return "none", None
    return "quart", quart


_WEB_BACKEND, _WEB = _detect_web_backend()


def _require_backend() -> Any:
    if _WEB is None:  # pragma: no cover - 裸解释器
        raise AliceWebError("当前运行环境没有可用的 Web 框架", status_code=503)
    return _WEB


def _json(
    data: Any,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> Any:
    web = _require_backend()
    if _WEB_BACKEND == "astrbot":
        return web.json_response(data, status_code=status_code, headers=dict(headers or {}) or None)
    return web.Response(
        json.dumps(data, ensure_ascii=False),
        status=status_code,
        content_type="application/json; charset=utf-8",
        headers=dict(headers or {}),
    )


def _error(message: str, *, status_code: int = 400) -> Any:
    web = _require_backend()
    if _WEB_BACKEND == "astrbot":
        return web.error_response(message, status_code=status_code)
    return _json(
        {"status": "error", "message": message, "data": None},
        status_code=status_code,
    )


def _content_disposition(filename: str, *, inline: bool = False) -> str:
    fallback = filename.encode("ascii", "ignore").decode("ascii") or "image.bin"
    quoted = quote(filename, safe="")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


def _binary(
    data: bytes,
    *,
    content_type: str,
    filename: str,
    inline: bool = False,
) -> Any:
    web = _require_backend()
    headers = {
        "Content-Disposition": _content_disposition(filename, inline=inline),
        "Content-Length": str(len(data)),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if _WEB_BACKEND == "astrbot":
        return web.stream_response(iter([data]), content_type=content_type, headers=headers)
    return web.Response(data, content_type=content_type, headers=headers)


def _request_obj() -> Any:
    return _require_backend().request


def _query(key: str, default: Any = None) -> Any:
    holder = _request_obj()
    bag = holder.query if _WEB_BACKEND == "astrbot" else holder.args
    return bag.get(key, default)


async def _json_body() -> Mapping[str, Any]:
    holder = _request_obj()
    if _WEB_BACKEND == "astrbot":
        payload = await holder.json(default={})
    else:  # pragma: no cover - 旧运行时
        payload = await holder.get_json(silent=True)
    if isinstance(payload, Mapping):
        return payload
    raise AliceWebError("请求体必须是 JSON 对象")


async def _uploaded_file() -> tuple[bytes, str]:
    holder = _request_obj()
    if _WEB_BACKEND == "astrbot":
        files = await holder.files()
    else:  # pragma: no cover - 旧运行时
        files = await holder.files
    upload = files.get("file")
    if upload is None:
        raise AliceWebError("没有收到文件")
    chunk = upload.read()
    if inspect.isawaitable(chunk):
        chunk = await chunk
    if not isinstance(chunk, bytes):
        chunk = bytes(chunk or b"")
    return chunk, str(getattr(upload, "filename", "") or "image")


class AliceWebApi:
    """把 Dashboard bridge 的调用映射到 AliceWebService。

    每个 handler 都是无位置参数的协程，正好符合 Context.register_web_api 的期望；
    纯 CPU 的图片处理都推到工作线程，避免拖住 Dashboard 的事件循环。
    """

    def __init__(self, service: AliceWebService, *, logger: Any = None) -> None:
        self._service = service
        self._logger = logger

    # -- 基础设施 ---------------------------------------------------------
    @property
    def available(self) -> bool:
        return _WEB_BACKEND != "none"

    @property
    def backend(self) -> str:
        return _WEB_BACKEND

    @property
    def service(self) -> AliceWebService:
        return self._service

    def routes(self) -> list[tuple[str, Callable[[], Awaitable[Any]], list[str], str]]:
        """返回 (route, handler, methods, description) 四元组供插件注册。"""

        prefix = f"/{PLUGIN_ID}"
        return [
            (f"{prefix}/meta", self.get_meta, ["GET"], "爱丽丝图片助手 · 能力清单与限额"),
            (f"{prefix}/health", self.get_health, ["GET"], "爱丽丝图片助手 · 运行体检"),
            (f"{prefix}/config", self.get_config, ["GET"], "爱丽丝图片助手 · 读取配置"),
            (f"{prefix}/config", self.post_config, ["POST"], "爱丽丝图片助手 · 保存配置"),
            (f"{prefix}/commands", self.get_commands, ["GET"], "爱丽丝图片助手 · 指令总表"),
            (f"{prefix}/search", self.post_search, ["POST"], "爱丽丝图片助手 · 试跑找图"),
            (f"{prefix}/reverse", self.post_reverse, ["POST"], "爱丽丝图片助手 · 试跑溯源"),
            (f"{prefix}/upload", self.post_upload, ["POST"], "爱丽丝图片助手 · 上传待溯源图片"),
            (f"{prefix}/image", self.get_image, ["GET"], "爱丽丝图片助手 · 取回图片"),
            (f"{prefix}/state", self.get_state, ["GET"], "爱丽丝图片助手 · 读取界面偏好"),
            (f"{prefix}/state", self.post_state, ["POST"], "爱丽丝图片助手 · 保存界面偏好"),
        ]

    def _log(self, message: str) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            logger.warning(message)
        except Exception:  # pragma: no cover - 日志不该反过来搞崩请求
            pass

    async def _guard(self, action: Callable[[], Awaitable[Any]]) -> Any:
        try:
            return await action()
        except AliceWebError as error:
            return _error(error.message, status_code=error.status_code)
        except (OSError, ValueError, KeyError, TypeError) as error:
            self._log(f"[{PLUGIN_ID}] web api 拒绝了一个请求: {error!r}")
            return _error("处理失败了，换个参数再试试")
        except Exception as error:  # pragma: no cover - 兜底
            self._log(f"[{PLUGIN_ID}] web api 崩了: {error!r}")
            return _error("插件内部错误，请查看 AstrBot 日志", status_code=500)

    @staticmethod
    async def _offload(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """把阻塞调用挪到工作线程。"""

        if kwargs:
            return await asyncio.to_thread(lambda: func(*args, **kwargs))
        return await asyncio.to_thread(func, *args)

    # -- handlers ---------------------------------------------------------
    async def get_meta(self) -> Any:
        async def run() -> Any:
            payload = await self._offload(self._service.meta)
            payload["backend"] = _WEB_BACKEND
            return _json(payload)

        return await self._guard(run)

    async def get_health(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.health())

        return await self._guard(run)

    async def get_config(self) -> Any:
        async def run() -> Any:
            return _json(await self._offload(self._service.config_payload))

        return await self._guard(run)

    async def post_config(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            changes = body.get("changes")
            if not isinstance(changes, Mapping):
                raise AliceWebError("changes 必须是「配置路径 → 值」的对象")
            return _json(await self._offload(self._service.apply_config, changes))

        return await self._guard(run)

    async def get_commands(self) -> Any:
        async def run() -> Any:
            return _json(await self._offload(self._service.commands))

        return await self._guard(run)

    async def post_search(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.search(await _json_body()))

        return await self._guard(run)

    async def post_reverse(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.reverse(await _json_body()))

        return await self._guard(run)

    async def post_upload(self) -> Any:
        async def run() -> Any:
            data, filename = await _uploaded_file()
            return _json(await self._service.adopt_upload(data, filename))

        return await self._guard(run)

    async def get_image(self) -> Any:
        async def run() -> Any:
            token = str(_query("token", "") or "").strip()
            if not token:
                raise AliceWebError("缺少 token")
            inline = str(_query("inline", "") or "").strip() not in {"", "0", "false"}
            data, mime, filename = self._service.image(token)
            return _binary(data, content_type=mime, filename=filename, inline=inline)

        return await self._guard(run)

    async def get_state(self) -> Any:
        async def run() -> Any:
            return _json(await self._service.load_state())

        return await self._guard(run)

    async def post_state(self) -> Any:
        async def run() -> Any:
            body = await _json_body()
            return _json(await self._service.save_state(body.get("state")))

        return await self._guard(run)


__all__ = [
    "PAGE_NAME",
    "PLUGIN_ID",
    "PLUGIN_VERSION",
    "AliceWebApi",
    "AliceWebError",
    "AliceWebService",
    "ImageCache",
    "WebUiEvent",
    "collect_image_sources",
    "make_preview",
    "probe_image",
]
