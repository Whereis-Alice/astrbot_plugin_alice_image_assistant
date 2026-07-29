import asyncio

from astrbot.api import logger
from pixivpy3 import ByPassSniApi, PixivError, AppPixivAPI


class PixivClientWrapper:
    """Pixiv API 客户端包装器，处理认证和定期刷新 Token"""

    def __init__(self, pixiv_config):
        self.pixiv_config = pixiv_config
        self._refresh_task: asyncio.Task | None = None
        self._auth_lock = asyncio.Lock()
        self._bypass_resolution_attempted = False

        # 根据是否配置代理选择不同的 API 客户端
        if pixiv_config.proxy:
            # 有代理时使用标准 AppPixivAPI
            self.client_api = AppPixivAPI(**pixiv_config.get_requests_kwargs())
            logger.info("Pixiv 插件：使用代理模式 (AppPixivAPI)")
        elif pixiv_config.api_proxy_host:
            # 使用 API 反代服务器
            self.client_api = AppPixivAPI()
            self.client_api.hosts = f"https://{pixiv_config.api_proxy_host}"
            logger.info(
                f"Pixiv 插件：使用 API 反代模式 ({pixiv_config.api_proxy_host})"
            )
        else:
            # 尝试多种直连方案
            self.client_api = self._create_direct_client()

    def _create_direct_client(self):
        """创建可直连也可按需切换 DoH 的客户端，不在插件加载阶段联网。"""
        logger.info("Pixiv 插件：网络探测已延迟到首次认证，插件加载不会被阻塞。")
        return ByPassSniApi()

    def _require_appapi_hosts_with_cn_doh(
        self, api, hostname: str = "app-api.secure.pixiv.net", timeout: int = 3
    ) -> str | bool:
        """使用国内可用的 DoH 服务器解析 Pixiv hosts"""
        import requests

        # 优先使用国内 DoH 服务器
        doh_urls = [
            "https://doh.pub/dns-query",  # 腾讯 DoH（国内可用）
            "https://dns.alidns.com/dns-query",  # 阿里 DoH（可能可用）
            "https://1.0.0.1/dns-query",  # Cloudflare 备选
            "https://1.1.1.1/dns-query",  # Cloudflare 主
            "https://doh.dns.sb/dns-query",  # DNS.sb
        ]

        headers = {"Accept": "application/dns-json"}
        params = {
            "name": hostname,
            "type": "A",
            "do": "false",
            "cd": "false",
        }

        for url in doh_urls:
            try:
                response = requests.get(
                    url, headers=headers, params=params, timeout=timeout
                )
                if response.status_code == 200:
                    data = response.json()
                    if "Answer" in data and data["Answer"]:
                        ip = data["Answer"][0]["data"]
                        api.hosts = f"https://{ip}"
                        return api.hosts
            except Exception:
                continue

        return False

    @staticmethod
    def _is_refresh_token_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return "check refresh_token" in text or "invalid_grant" in text

    def _authenticate_sync(self, refresh_token: str) -> None:
        """在线程中认证；直连网络失败时只尝试一次 DoH 回退。"""
        try:
            self.client_api.auth(refresh_token=refresh_token)
            return
        except Exception as direct_error:
            should_try_bypass = (
                isinstance(self.client_api, ByPassSniApi)
                and not self._bypass_resolution_attempted
                and not self._is_refresh_token_error(direct_error)
            )
            if not should_try_bypass:
                raise

            self._bypass_resolution_attempted = True
            logger.warning("Pixiv 插件：直连认证失败，尝试 DoH 解析后重试。")
            hosts = self._require_appapi_hosts_with_cn_doh(self.client_api)
            if not hosts:
                raise direct_error
            logger.info("Pixiv 插件：已切换 ByPassSniApi，hosts=%s", hosts)
            self.client_api.auth(refresh_token=refresh_token)

    async def authenticate(self) -> bool:
        """尝试使用配置的凭据进行 Pixiv API 认证"""
        refresh_token = self.pixiv_config.refresh_token
        if not refresh_token:
            logger.error("Pixiv 插件：未提供有效的 Refresh Token，无法进行认证。")
            return False

        try:
            async with self._auth_lock:
                await asyncio.to_thread(self._authenticate_sync, refresh_token)
                return True
        except Exception as e:
            logger.error(
                f"Pixiv 插件：认证/刷新时发生错误 - 异常类型: {type(e)}, 错误信息: {e}"
            )
            return False

    async def periodic_token_refresh(self):
        """定期尝试使用 refresh_token 进行认证以保持其活性"""
        while True:
            try:
                # 先等待指定间隔
                wait_seconds = self.pixiv_config.refresh_interval * 60
                if wait_seconds <= 0:
                    logger.info("Pixiv Token 刷新任务：刷新间隔已关闭，任务退出。")
                    break
                logger.debug(
                    f"Pixiv Token 刷新任务：等待 {self.pixiv_config.refresh_interval} 分钟 ({wait_seconds} 秒)..."
                )
                await asyncio.sleep(wait_seconds)

                # 检查 refresh_token 是否已配置
                current_refresh_token = self.pixiv_config.refresh_token
                if not current_refresh_token:
                    logger.warning(
                        "Pixiv Token 刷新任务：未配置 Refresh Token，跳过本次刷新。"
                    )
                    continue

                logger.info("Pixiv Token 刷新任务：尝试使用 Refresh Token 进行认证...")
                try:
                    if not await self.authenticate():
                        continue
                    logger.info("Pixiv Token 刷新任务：认证调用成功。")

                except PixivError as pe:
                    logger.error(
                        f"Pixiv Token 刷新任务：认证时发生 Pixiv API 错误 - {pe}"
                    )
                except Exception as e:
                    logger.error(
                        f"Pixiv Token 刷新任务：认证时发生未知错误 - {type(e).__name__}: {e}"
                    )
                    import traceback

                    logger.error(traceback.format_exc())

            except asyncio.CancelledError:
                logger.info("Pixiv Token 刷新任务：任务被取消，停止刷新。")
                break
            except Exception as loop_e:
                logger.error(
                    f"Pixiv Token 刷新任务：循环中发生意外错误 - {loop_e}，将在下次间隔后重试。"
                )
                import traceback

                logger.error(traceback.format_exc())

    def start_refresh_task(self) -> asyncio.Task | None:
        """启动后台刷新任务并返回任务句柄（若已启动则复用原任务）。"""
        if not self.pixiv_config.refresh_token:
            logger.info("Pixiv 插件：未配置 Refresh Token，不启动自动刷新任务。")
            return None
        if self.pixiv_config.refresh_interval <= 0:
            logger.info("Pixiv 插件：Refresh Token 自动刷新已禁用。")
            return None

        if self._refresh_task and not self._refresh_task.done():
            return self._refresh_task

        self._refresh_task = asyncio.create_task(self.periodic_token_refresh())
        logger.info(
            f"Pixiv 插件：已启动 Refresh Token 自动刷新任务，间隔 {self.pixiv_config.refresh_interval} 分钟。"
        )
        return self._refresh_task

    async def stop_refresh_task(self) -> None:
        """停止后台刷新任务。"""
        if not self._refresh_task or self._refresh_task.done():
            return

        self._refresh_task.cancel()
        try:
            await self._refresh_task
        except asyncio.CancelledError:
            logger.info("Pixiv Token 刷新任务已成功取消。")
        except Exception as e:
            logger.error(f"等待 Pixiv Token 刷新任务取消时发生错误: {e}")

    async def call_pixiv_api(self, func, *args, **kwargs):
        """异步调用 Pixiv API 的辅助方法"""
        return await asyncio.to_thread(func, *args, **kwargs)
