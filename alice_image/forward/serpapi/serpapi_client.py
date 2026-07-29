"""SerpApi 客户端：统一封装请求、多 Key 轮询与错误处理，供 google_images/google_lens 共用。"""

from __future__ import annotations

import asyncio
import json
import time

import aiohttp

from astrbot.api import logger

from .image_utils import HTTP_TIMEOUT_SECONDS, HttpService

SERPAPI_SEARCH_URL = "https://serpapi.com/search.json"
# Key 被标记为耗尽后的缓存时长（秒），过后可再次尝试
QUOTA_CACHE_TTL = 60

# Key 级不可用的权威信号是 HTTP 状态码（见 https://serpapi.com/api-status-and-error-codes）：
#   401 无有效 Key，403 账号被停用/无权限，429 触发每小时吞吐上限或额度耗尽。
# 命中任一即冷却当前 Key 并轮换到下一个；其余状态（如 400 参数错误）换 Key 无济于事。
_KEY_UNAVAILABLE_STATUS = frozenset({401, 403, 429})


class SerpApiError(Exception):
    """SerpApi 调用失败（无可用 Key、鉴权失败、返回错误等）。"""


class SerpApiClient:
    """带多 Key 轮询的 SerpApi 客户端。

    - 401/403/429（鉴权失败 / 账号停用 / 额度或限频耗尽）：标记该 Key 暂时不可用并自动切换到下一个 Key；
    - 其它业务错误（如 400 参数错误）：直接抛出，不再重试其它 Key。
    """

    def __init__(
        self,
        api_keys: list[str] | None,
        http: HttpService,
        timeout: int = HTTP_TIMEOUT_SECONDS,
    ):
        self.api_keys = [str(k).strip() for k in (api_keys or []) if str(k).strip()]
        self.http = http
        self.timeout = timeout
        self._idx = 0
        self._lock = asyncio.Lock()
        # {api_key: 标记耗尽时的时间戳}
        self._exhausted: dict[str, float] = {}

    def has_keys(self) -> bool:
        return bool(self.api_keys)

    async def _pick_key(self) -> str | None:
        """轮询选择一个未被标记耗尽的 Key。"""
        async with self._lock:
            now = time.time()
            for k in [
                k for k, ts in self._exhausted.items() if now - ts > QUOTA_CACHE_TTL
            ]:
                del self._exhausted[k]

            n = len(self.api_keys)
            for i in range(n):
                idx = (self._idx + i) % n
                key = self.api_keys[idx]
                if key in self._exhausted:
                    continue
                self._idx = (idx + 1) % n
                return key
            return None

    async def _mark_exhausted(self, key: str) -> None:
        async with self._lock:
            self._exhausted[key] = time.time()
            logger.debug(f"[alice_image_serpapi] 已标记 Key ...{key[-4:]} 暂不可用")

    async def get(self, engine: str, params: dict) -> dict:
        """向 SerpApi 发起请求并返回解析后的 JSON。

        engine 为引擎名（google_images/google_lens）；params 为其余查询参数
        （自动剔除空值并附加 engine 与 api_key）。无可用 Key、全部 Key 失败或返回
        业务错误时抛出 SerpApiError。
        """
        if not self.api_keys:
            raise SerpApiError(
                "未配置 SerpApi API Key，请在插件配置 api_keys.serpapi_keys 中填写。"
            )

        last_err: Exception | None = None
        session = await self.http.session()
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        proxy = self.http.proxy_url

        for _ in range(len(self.api_keys)):
            key = await self._pick_key()
            if key is None:
                if last_err is None:
                    last_err = SerpApiError(
                        "全部 Key 暂时处于冷却中（额度耗尽/鉴权失败），请稍后重试。"
                    )
                break

            query = {k: v for k, v in params.items() if v not in (None, "")}
            query["engine"] = engine
            query["api_key"] = key

            try:
                async with session.get(
                    SERPAPI_SEARCH_URL, params=query, timeout=timeout, proxy=proxy
                ) as resp:
                    status = resp.status
                    text = await resp.text()

                if status in _KEY_UNAVAILABLE_STATUS:
                    await self._mark_exhausted(key)
                    last_err = SerpApiError(
                        f"Key ...{key[-4:]} 不可用 (HTTP {status})：鉴权失败/账号停用/额度或限频耗尽"
                    )
                    continue

                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    last_err = SerpApiError(
                        f"SerpApi 返回非 JSON (HTTP {status}): {text[:160]}"
                    )
                    continue

                if not isinstance(data, dict):
                    return data

                err = data.get("error")
                if err:
                    # 搜索成功但无结果时，SerpApi 仍返回 HTTP 200 且带 error 文案
                    # （search_metadata.status == "Success"，如 "Google hasn't returned any results"）。
                    # 这属于"查无结果"而非失败，原样返回交由上层按空结果友好处理，绝不能抛异常。
                    search_status = (data.get("search_metadata") or {}).get("status")
                    if search_status == "Success":
                        return data
                    # 其余 error（status == "Error"、400 参数错误等）才是真失败；额度/限频已由
                    # 上面的 HTTP 状态码（429 等）权威判定，此处不再靠错误文案猜测。
                    raise SerpApiError(str(err))

                return data

            except SerpApiError:
                raise
            except asyncio.TimeoutError:
                last_err = SerpApiError("SerpApi 请求超时")
                continue
            except Exception as e:  # noqa: BLE001 - 网络异常换 Key 重试
                last_err = e
                continue

        raise SerpApiError(f"所有 SerpApi Key 均不可用：{last_err}")
