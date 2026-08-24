# -*- coding: utf-8 -*-
"""
===================================
Telegram Long Polling 客户端
===================================

为 Telegram 补齐"接收指令"的双向能力：用 getUpdates 长轮询持续拉取用户发来的消息，
封装为系统统一的 ``BotMessage``，交给 ``CommandDispatcher.dispatch_async`` 分发，
执行结果通过 ``TelegramSender.send_to_telegram`` 回复到原会话。

特性：
1. offset 递增 + 长轮询 timeout，避免重复/漏拉消息
2. 断线自动指数退避重连
3. 退出标记实现优雅停表；后台跑在独立 daemon 线程的独立事件循环里，
   与 FastAPI / 主线程的事件循环互不干扰
4. 支持从现有配置读取 HTTP / SOCKS5 代理（复用 `config.http_proxy` / `config.https_proxy`，
   未显式配置时退回 requests 读取的 HTTP(S)_PROXY 环境变量）
"""

import asyncio
import atexit
import logging
import threading
import time
from typing import Dict, List, Optional

import requests

from bot.models import BotMessage, ChatType

logger = logging.getLogger(__name__)

TELEGRAM_POLLING_AVAILABLE = True

# getUpdates 长轮询秒数。Telegram 允许 0-60，传 it 表示等待新消息最多该秒数。
DEFAULT_POLL_TIMEOUT = 50
# 断线重连初始退避（秒），指数增长并封顶
DEFAULT_RETRY_DELAY = 5.0
MAX_RETRY_DELAY = 60.0

_API_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramPollingError(Exception):
    """Telegram API 返回业务错误的封装"""


def _build_proxies(config) -> Optional[Dict[str, str]]:
    """从配置读取代理。

    复用现有 `http_proxy` / `https_proxy` 配置项；两者都为空时返回 None，
    让 requests 自行读取请求级配置写入的环境变量（HTTP_PROXY/HTTPS_PROXY）。
    """
    http_proxy = getattr(config, "http_proxy", None)
    https_proxy = getattr(config, "https_proxy", None)
    proxies: Dict[str, str] = {}
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    return proxies or None


class TelegramPollingClient:
    """Telegram getUpdates 长轮询客户端，为 Telegram 提供消息接收能力。"""

    def __init__(self, config):
        self._token = getattr(config, "telegram_bot_token", None)
        self._proxies = _build_proxies(config)
        # Telegram 非代理命令（webhook 冲突/消灭轮询等），用 None 表示已删除
        self._timeout = DEFAULT_POLL_TIMEOUT
        self._retry_delay = DEFAULT_RETRY_DELAY

        self._offset: Optional[int] = None
        self._bot_username: Optional[str] = None

        self._running = threading.Event()
        self._running.set()

        # 惰性初始化，避免 import 阶段产生不必要的依赖 / 循环引用
        self._dispatcher = None
        self._sender = None

    # ------------------------------------------------------------------ #
    #  运行时状态                                                     #
    # ------------------------------------------------------------------ #

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    def stop(self) -> None:
        """请求优雅停止；在下一个轮询循环边界退出。"""
        self._running.clear()

    # ------------------------------------------------------------------ #
    #  内部 API                                                            #
    # ------------------------------------------------------------------ #

    def _api_url(self, method: str) -> str:
        return _API_BASE.format(token=self._token, method=method)

    def _request(self, method: str, **params) -> dict:
        """同步执行一次 Telegram Bot API 调用，返回 JSON result 或抛异常。"""
        url = self._api_url(method)
        try:
            resp = requests.get(url, params=params, proxies=self._proxies, timeout=self._timeout + 5)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ProxyError) as exc:
            raise TelegramPollingError(f"连接 Telegram API 失败: {exc}") from exc

        if resp.status_code != 200:
            raise TelegramPollingError(
                f"Telegram API HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise TelegramPollingError(f"Telegram API 响应不是 JSON: {exc}") from exc

        if not payload.get("ok"):
            desc = payload.get("description", "未知错误")
            raise TelegramPollingError(f"Telegram API 返回错误: {desc}")
        return payload.get("result")

    def _get_bot_username(self) -> Optional[str]:
        """获取 bot 用户名，用于识别群聊里 @bot 前缀的提及。失败时返回 None。

        缓存结果避免每次轮询都调用 getMe。
        """
        if self._bot_username:
            return self._bot_username
        try:
            me = self._request("getMe")
        except TelegramPollingError as exc:
            logger.warning("[TelegramPolling] 获取 bot 用户名失败: %s", exc)
            return None
        username = (me or {}).get("username")
        self._bot_username = username
        return username

    def _get_updates(self) -> List[dict]:
        """带 offset 与长轮询 timeout 拉取一次 updates。"""
        params: Dict[str, object] = {
            "timeout": self._timeout,
            "allowed_updates": '["message"]',
        }
        if self._offset is not None:
            params["offset"] = self._offset

        updates = self._request("getUpdates", **params)
        return updates if isinstance(updates, list) else []

    # ------------------------------------------------------------------ #
    #  update -> BotMessage                                                #
    # ------------------------------------------------------------------ #

    def parse_message(self, raw: dict) -> Optional[BotMessage]:
        """把一条 Telegram update 解析为 ``BotMessage``。

        仅处理普通 ``message``（含文本）；回调、编辑、频道帖等非目标类型返回 None。
        """
        if not isinstance(raw, dict):
            return None
        msg = raw.get("message")
        if not isinstance(msg, dict):
            return None

        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return None

        user = msg.get("from") or {}
        chat = msg.get("chat") or {}

        chat_type_raw = chat.get("type", "unknown")
        if chat_type_raw == "private":
            chat_type = ChatType.PRIVATE
        elif chat_type_raw in ("group", "supergroup"):
            chat_type = ChatType.GROUP
        else:
            chat_type = ChatType.UNKNOWN

        content = text
        mentioned = False
        # 群聊里识别 @bot 提及：剥掉开头的 @username，便于命令解析与 NL 路由
        if chat_type == ChatType.GROUP:
            bot_username = self._get_bot_username()
            if bot_username:
                mention = f"@{bot_username}"
                if mention in text:
                    mentioned = True
                    content = text.replace(mention, "").strip()
            else:
                # 拿不到 bot 用户名时，保守匹配任意开头的 @ 提及
                stripped = text.lstrip()
                if stripped.startswith("@"):
                    mentioned = True
                    rest = stripped.split(None, 1)
                    content = rest[1] if len(rest) > 1 else ""

        user_id = user.get("id")
        chat_id = chat.get("id")
        if user_id is None or chat_id is None:
            return None

        return BotMessage(
            platform="telegram",
            message_id=str(msg.get("message_id", "")),
            user_id=str(user_id),
            user_name=user.get("username") or user.get("first_name") or str(user_id),
            chat_id=str(chat_id),
            chat_type=chat_type,
            content=content.strip() or text,
            raw_content=text,
            mentioned=mentioned,
            timestamp=msg.get("date"),
            raw_data=raw,
        )

    # ------------------------------------------------------------------ #
    #  分发与回信                                                        #
    # ------------------------------------------------------------------ #

    async def handle_update(self, update: dict) -> None:
        message = self.parse_message(update)
        if message is None:
            return

        dispatcher = self._get_dispatcher()
        try:
            response = await dispatcher.dispatch_async(message)
        except Exception as exc:  # 分发层已兜底，这里仅防御性记录
            logger.error("[TelegramPolling] 命令分发异常: %s", exc)
            return

        if not response or not getattr(response, "text", ""):
            return

        sender = self._get_sender()
        try:
            await asyncio.to_thread(
                sender.send_to_telegram,
                response.text,
                chat_id=message.chat_id,
            )
        except Exception as exc:
            logger.error("[TelegramPolling] 回复消息失败: %s", exc)

    # ------------------------------------------------------------------ #
    #  后台轮询循环                                                       #
    # ------------------------------------------------------------------ #

    async def run_forever(self) -> None:
        """阻塞式长轮询主循环；断线时指数退避重连，直到 ``stop()``。"""
        logger.info("[TelegramPolling] Telegram 轮询客户端启动")
        while self.is_running:
            try:
                updates = await asyncio.to_thread(self._get_updates)
            except TelegramPollingError as exc:
                logger.warning("[TelegramPolling] 拉取更新失败（%s），%.1fs 后重试",
                               exc, self._retry_delay)
                if self._retry_delay < MAX_RETRY_DELAY:
                    self._retry_delay = min(MAX_RETRY_DELAY, self._retry_delay * 2)
                await self._sleep(self._retry_delay)
                continue

            # 正常收到结果，重置退避
            self._retry_delay = DEFAULT_RETRY_DELAY

            if updates:
                # 严格递增 offset：处理完一批后让 Telegram 从这批之后开始
                newest = max(u.get("update_id", 0) for u in updates)
                self._offset = newest + 1

                for update in updates:
                    if not self.is_running:
                        break
                    try:
                        await self.handle_update(update)
                    except Exception as exc:  # 单条失败不影响后续
                        logger.error("[TelegramPolling] 处理 update 异常: %s", exc)
            else:
                # 空结果：长轮询通常仅在超时或有新消息时返回；这里再加一个可
                # 中断的短等待，避免连接异常导致空结果热循环打爆 Telegram API。
                await self._sleep(1.0)
        logger.info("[TelegramPolling] Telegram 轮询客户端已停止")

    async def _sleep(self, seconds: float) -> None:
        """带停止标记的可中断 sleep，保证 stop() 最多 1 秒内退出。"""
        deadline = time.monotonic() + seconds
        while self.is_running and time.monotonic() < deadline:
            await asyncio.sleep(1.0)

    def _get_dispatcher(self):
        if self._dispatcher is None:
            from bot.dispatcher import get_dispatcher
            self._dispatcher = get_dispatcher()
        return self._dispatcher

    def _get_sender(self):
        if self._sender is None:
            from src.notification_sender.telegram_sender import TelegramSender
            from src.config import get_config
            self._sender = TelegramSender(get_config())
        return self._sender


# ---------------------------------------------------------------------- #
#  后台启动 / 停止                                                        #
# ---------------------------------------------------------------------- #

_polling_client: Optional[TelegramPollingClient] = None
_polling_thread: Optional[threading.Thread] = None


def _run_client_event_loop(client: TelegramPollingClient) -> None:
    """在后台线程里跑一个独立事件循环，避免与主/FastAPI 循环冲突。"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(client.run_forever())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:  # pragma: no cover - 清理兜底
            pass
        loop.close()


def start_telegram_polling_background() -> bool:
    """当配置了 Telegram Token 且开关开启时，以 daemon 线程启动轮询客户端。

    Returns:
        是否成功进入后台运行（Token 缺失、开关关闭或已在运行返回 False/True 视情况）。
    """
    global _polling_client, _polling_thread

    from src.config import get_config
    config = get_config()

    if not getattr(config, "telegram_bot_token", None):
        logger.debug("[TelegramPolling] 未配置 TELEGRAM_BOT_TOKEN，跳过启动")
        return False

    if not getattr(config, "telegram_polling_enabled", True):
        logger.info("[TelegramPolling] TELEGRAM_POLLING_ENABLED=false，跳过启动")
        return False

    if _polling_client is not None and _polling_client.is_running:
        return True

    client = TelegramPollingClient(config)
    thread = threading.Thread(
        target=_run_client_event_loop,
        args=(client,),
        name="telegram-polling",
        daemon=True,
    )
    _polling_client = client
    _polling_thread = thread
    thread.start()
    logger.info("[TelegramPolling] Telegram 轮询在后台线程启动")
    atexit.register(stop_telegram_polling)
    return True


def stop_telegram_polling() -> None:
    """优雅停止轮询客户端（也由 atexit 兜底调用）。"""
    global _polling_client
    client = _polling_client
    if client is not None:
        client.stop()