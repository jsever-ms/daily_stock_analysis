# -*- coding: utf-8 -*-
"""
Telegram 发送提醒服务

职责：
1. 通过 Telegram Bot API 发送 文本消息
2. 通过 Telegram Bot API 发送 图片消息
"""
import logging
from typing import Optional
import requests
import time

from src.config import Config
from src.formatters import format_telegram_markdown, strip_hidden_markdown_metadata


logger = logging.getLogger(__name__)


class TelegramSender:

    def __init__(self, config: Config):
        """
        初始化 Telegram 配置

        Args:
            config: 配置对象
        """
        self._telegram_config = {
            'bot_token': getattr(config, 'telegram_bot_token', None),
            'chat_id': getattr(config, 'telegram_chat_id', None),
            'message_thread_id': getattr(config, 'telegram_message_thread_id', None),
        }
        # Token 有效性缓存：getMe 验证成功后不再重复验证，避免每次发送多一次 API 调用
        self._token_verified: bool = False
        # 最近一次 getMe / sendMessage 的真实 HTTP 状态（供测试脚本与诊断输出使用）
        # - None 表示网络层失败（连接超时/拒绝）或尚未请求
        # - 其余为 Telegram API 返回的具体 HTTP 状态码
        self.last_get_me_status: Optional[int] = None
        self.last_send_message_status: Optional[int] = None

    @staticmethod
    def _safe_token_preview(token: str) -> str:
        """生成 Token 安全预览：只保留前 4 位与后 4 位，中间打码。

        禁止把完整 Token 打进日志。
        """
        if not token:
            return ""
        if len(token) <= 8:
            return "*" * len(token)
        return f"{token[:4]}{'*' * (len(token) - 8)}{token[-4:]}"

    def _log_token_diagnostics(self, bot_token: str) -> None:
        """打印 Token 安全诊断信息（不含完整值），用于定位 401 类认证问题。

        覆盖常见 Secret 粘贴事故：前后空格、尾随换行（CRLF 粘贴）、
        引号包裹、缺少冒号（标准 Bot Token 为 ``<bot_id>:<hash>``）。
        """
        has_space = " " in bot_token
        has_newline = any(c in bot_token for c in ("\n", "\r"))
        has_quote = bot_token[:1] in {"'", '"'} or bot_token[-1:] in {"'", '"'}
        has_colon = ":" in bot_token
        logger.warning(
            "Telegram token: present=%s, len=%d, space=%s, newline=%s, quote=%s, "
            "colon=%s, preview=%s",
            bool(bot_token), len(bot_token), has_space, has_newline, has_quote,
            has_colon, self._safe_token_preview(bot_token),
        )
        if has_space or has_newline or has_quote:
            logger.warning(
                "Telegram Token 疑似包含多余空白/换行/引号，请检查 Secret 原始值"
                "（GitHub Secret 会原样保留粘贴内容）"
            )
        if not has_colon:
            logger.warning(
                "Telegram Token 不含冒号，不符合标准格式 <bot_id>:<hash>，"
                "请确认复制的是 BotFather 完整 Token"
            )

    def _verify_token_via_get_me(self, bot_token: str) -> bool:
        """发送消息前用同一 Token 调用 getMe 验证身份。

        - getMe 成功 → 缓存结果，后续发送不再重复验证
        - getMe 返回 401 → Token 无效或未正确加载，明确报错并阻止本次发送
        - 网络异常 / 服务端 5xx → 记录警告但不阻止 sendMessage（避免误伤网络抖动）
        """
        if self._token_verified:
            return True

        api_url = f"https://api.telegram.org/bot{bot_token}/getMe"
        try:
            response = requests.get(api_url, timeout=10)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            self.last_get_me_status = None
            logger.warning(f"Telegram getMe 验证请求异常（不阻塞发送）: {e}")
            return True

        self.last_get_me_status = response.status_code

        if response.status_code == 200 and response.json().get('ok'):
            username = response.json().get('result', {}).get('username', 'unknown')
            logger.info(f"Telegram Token 验证成功 (getMe OK, bot=@{username})")
            self._token_verified = True
            return True

        if response.status_code == 401:
            # 撤销后的 Token 调用 Bot API 返回 401 Unauthorized
            logger.error("Telegram Token 无效或未正确加载（getMe 返回 401 Unauthorized）")
            logger.error(
                "请确认：1) BotFather 生成的最新 Token 已更新到 GitHub Secret "
                "TELEGRAM_BOT_TOKEN；2) 旧 Token 已在 BotFather /revoke 后不再被任何"
                " Secret/环境变量引用；3) Secret 值前后无多余空格、换行或引号"
            )
            self._log_token_diagnostics(bot_token)
            return False

        logger.warning(
            f"Telegram getMe 验证返回非预期状态 HTTP {response.status_code}"
            f"（不阻塞发送）: {response.text[:200]}"
        )
        return True

    def verify_token(self) -> bool:
        """公开的 Token 验证入口：复用 ``_verify_token_via_get_me`` 同一底层逻辑。

        供独立测试脚本调用；验证后可通过 ``self.last_get_me_status`` 获取真实 HTTP 状态。
        """
        bot_token = self._telegram_config['bot_token']
        if not bot_token:
            self.last_get_me_status = None
            return False
        return self._verify_token_via_get_me(bot_token)

    def _is_telegram_configured(self) -> bool:
        """检查 Telegram 配置是否完整"""
        return bool(self._telegram_config['bot_token'] and self._telegram_config['chat_id'])

    def send_to_telegram(
        self,
        content: str,
        *,
        chat_id: Optional[str] = None,
        message_thread_id: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """
        推送消息到 Telegram 机器人

        Telegram Bot API 格式：
        POST https://api.telegram.org/bot<token>/sendMessage
        {
            "chat_id": "xxx",
            "text": "消息内容",
            "parse_mode": "Markdown"
        }

        Args:
            content: 消息内容（Markdown 格式）

        Returns:
            是否发送成功
        """
        target_chat_id = chat_id if chat_id is not None else self._telegram_config.get("chat_id")
        target_message_thread_id = (
            message_thread_id
            if message_thread_id is not None
            else self._telegram_config.get("message_thread_id")
        )

        if not (self._telegram_config["bot_token"] and target_chat_id):
            logger.warning("Telegram 配置不完整，跳过推送")
            return False

        bot_token = self._telegram_config['bot_token']
        chat_id = target_chat_id
        message_thread_id = target_message_thread_id

        # 发送前先用同一 Token 调用 getMe 验证身份；401 时直接明确报错，
        # 避免用无效 Token 反复重试 sendMessage 得到含糊的失败日志。
        if not self._verify_token_via_get_me(bot_token):
            return False

        try:
            # Telegram API 端点
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

            # Telegram 消息最大长度 4096 字符
            max_length = 4096

            sanitized_content = strip_hidden_markdown_metadata(content).strip()
            if not sanitized_content:
                logger.warning("Telegram 消息内容为空，跳过推送")
                return False

            telegram_content = self._convert_to_telegram_markdown(sanitized_content)

            if len(telegram_content) <= max_length:
                # 单条消息发送
                return self._send_telegram_message(
                    api_url,
                    chat_id,
                    sanitized_content,
                    message_thread_id,
                    timeout_seconds=timeout_seconds,
                )
            else:
                # 按 Markdown 转义后的最终 payload 分段，避免转义字符使请求超限
                return self._send_telegram_chunked(
                    api_url,
                    chat_id,
                    telegram_content,
                    max_length,
                    message_thread_id,
                    timeout_seconds=timeout_seconds,
                )

        except Exception as e:
            logger.error(f"发送 Telegram 消息失败: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return False

    def _send_telegram_message(
        self,
        api_url: str,
        chat_id: str,
        text: str,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
        markdown_converted: bool = False,
    ) -> bool:
        """Send a single Telegram message with exponential backoff retry (Fixes #287)"""
        # Convert Markdown to Telegram-compatible format
        telegram_text = text if markdown_converted else self._convert_to_telegram_markdown(text)

        payload = {
            "chat_id": chat_id,
            "text": telegram_text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }

        if message_thread_id:
            payload['message_thread_id'] = message_thread_id

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(api_url, json=payload, timeout=timeout_seconds or 10)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if attempt < max_retries:
                    delay = 2 ** attempt  # 2s, 4s
                    logger.warning(f"Telegram request failed (attempt {attempt}/{max_retries}): {e}, "
                                   f"retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                else:
                    self.last_send_message_status = None
                    logger.error(f"Telegram request failed after {max_retries} attempts: {e}")
                    return False

            self.last_send_message_status = response.status_code
            if response.status_code == 200:
                result = response.json()
                if result.get('ok'):
                    logger.info("Telegram 消息发送成功")
                    return True
                else:
                    error_desc = result.get('description', '未知错误')
                    logger.error(f"Telegram 返回错误: {error_desc}")

                    # If Markdown parsing failed, fall back to plain text
                    if self._should_fallback_to_plain_text(error_desc=error_desc):
                        if self._send_plain_text_fallback(api_url, payload, text, timeout_seconds=timeout_seconds):
                            return True

                    return False
            elif response.status_code == 429:
                # Rate limited — respect Retry-After header
                retry_after = int(response.headers.get('Retry-After', 2 ** attempt))
                if attempt < max_retries:
                    logger.warning(f"Telegram rate limited, retrying in {retry_after}s "
                                   f"(attempt {attempt}/{max_retries})...")
                    time.sleep(retry_after)
                    continue
                else:
                    logger.error(f"Telegram rate limited after {max_retries} attempts")
                    return False
            else:
                if attempt < max_retries and response.status_code >= 500:
                    delay = 2 ** attempt
                    logger.warning(f"Telegram server error HTTP {response.status_code} "
                                   f"(attempt {attempt}/{max_retries}), retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                if self._should_fallback_to_plain_text(response_text=response.text):
                    if self._send_plain_text_fallback(api_url, payload, text, timeout_seconds=timeout_seconds):
                        return True
                logger.error(f"Telegram 请求失败: HTTP {response.status_code}")
                logger.error(f"响应内容: {response.text}")
                return False

        return False

    @staticmethod
    def _should_fallback_to_plain_text(error_desc: str = "", response_text: str = "") -> bool:
        """Detect Telegram Markdown parsing failures that should retry as plain text."""
        haystack = f"{error_desc}\n{response_text}".lower()
        markers = (
            "can't parse entities",
            "can't parse entity",
            "can't find end of the entity",
            "parse entities",
            "parse_mode",
            "markdown",
        )
        return any(marker in haystack for marker in markers)

    def _send_plain_text_fallback(
        self,
        api_url: str,
        payload: dict,
        text: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """Retry Telegram send without parse_mode when Markdown parsing fails."""
        logger.info("Telegram Markdown 解析失败，尝试使用纯文本格式重新发送...")
        plain_payload = dict(payload)
        plain_payload.pop('parse_mode', None)
        plain_payload['text'] = text

        try:
            response = requests.post(api_url, json=plain_payload, timeout=timeout_seconds or 10)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            logger.error(f"Telegram plain-text fallback failed: {e}")
            return False

        if response.status_code == 200:
            try:
                result = response.json()
            except ValueError:
                logger.error("Telegram 纯文本回退失败: 响应不是有效 JSON")
                logger.error(f"响应内容: {response.text}")
                return False

            if result.get('ok'):
                logger.info("Telegram 消息发送成功（纯文本）")
                return True

            logger.error("Telegram 纯文本回退失败: Telegram API 返回 ok=false")
            logger.error(f"响应内容: {response.text}")
            return False

        logger.error(f"Telegram 纯文本回退失败: HTTP {response.status_code}")
        logger.error(f"响应内容: {response.text}")
        return False

    def _send_telegram_chunked(
        self,
        api_url: str,
        chat_id: str,
        content: str,
        max_length: int,
        message_thread_id: Optional[str] = None,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        """按已转换的 Telegram Markdown payload 分段发送长消息。"""
        # 按段落分割
        sections = content.split("\n---\n")
        delimiter = "\n---\n"
        delimiter_length = len(delimiter)

        current_chunk = []
        current_length = 0
        all_success = True
        chunk_index = 1

        def _flush_chunk() -> bool:
            nonlocal current_chunk, current_length, chunk_index, all_success
            if not current_chunk:
                return all_success

            chunk_content = "\n---\n".join(current_chunk)
            logger.info(f"发送 Telegram 消息块 {chunk_index}...")
            chunk_index += 1
            current_chunk = []
            current_length = 0
            if not self._send_telegram_message(
                api_url,
                chat_id,
                chunk_content,
                message_thread_id,
                timeout_seconds=timeout_seconds,
                markdown_converted=True,
            ):
                all_success = False
            return all_success

        def _split_long_section(section: str, limit: int) -> list[str]:
            if len(section) <= limit:
                return [section]
            chunks: list[str] = []
            for start in range(0, len(section), limit):
                chunks.append(section[start:start + limit])
            return chunks

        for section in sections:
            if len(section) > max_length:
                # 单段超限时强制切片，避免依赖“\\n---\\n”边界导致的整段超长发送
                if not _flush_chunk():
                    return False
                for long_chunk in _split_long_section(section, max_length):
                    logger.info(f"发送 Telegram 消息块 {chunk_index}...")
                    chunk_index += 1
                    if not self._send_telegram_message(
                        api_url,
                        chat_id,
                        long_chunk,
                        message_thread_id,
                        timeout_seconds=timeout_seconds,
                        markdown_converted=True,
                    ):
                        all_success = False
                continue

            additional_length = len(section)
            if current_chunk:
                additional_length += delimiter_length

            if current_length + additional_length > max_length:
                _flush_chunk()
                current_chunk = [section]
                current_length = len(section)
                continue

            current_chunk.append(section)
            current_length += additional_length

        # 发送最后一块
        if not _flush_chunk():
            return False

        return all_success

    def _send_telegram_photo(self, image_bytes: bytes) -> bool:
        """Send image via Telegram sendPhoto API (Issue #289)."""
        if not self._is_telegram_configured():
            return False
        bot_token = self._telegram_config['bot_token']
        chat_id = self._telegram_config['chat_id']
        message_thread_id = self._telegram_config.get('message_thread_id')
        if not self._verify_token_via_get_me(bot_token):
            return False
        api_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        try:
            data = {"chat_id": chat_id}
            if message_thread_id:
                data['message_thread_id'] = message_thread_id
            files = {"photo": ("report.png", image_bytes, "image/png")}
            response = requests.post(api_url, data=data, files=files, timeout=30)
            if response.status_code == 200 and response.json().get('ok'):
                logger.info("Telegram 图片发送成功")
                return True
            logger.error("Telegram 图片发送失败: %s", response.text[:200])
            return False
        except Exception as e:
            logger.error("Telegram 图片发送异常: %s", e)
            return False

    # 内部调试/阶段元信息行：Telegram 正文不展示（保留在日志与其他渠道）
    _INTERNAL_METADATA_LINE_PREFIXES = (
        "- 阶段：", "- 市场：", "- 触发来源：", "- 摘要来源：", "- 盘中数据提示：",
        "- 数据质量", "- 限制", "- 阶段:", "- 市场:", "- 触发来源:", "- 摘要来源:",
        "- data quality", "- limitation", "- phase:", "- trigger:",
    )

    def _drop_internal_metadata_lines(self, text: str) -> str:
        kept = [
            line for line in text.splitlines()
            if not line.strip().startswith(self._INTERNAL_METADATA_LINE_PREFIXES)
        ]
        return "\n".join(kept)

    def _convert_to_telegram_markdown(self, text: str) -> str:
        """
        将标准 Markdown 转换为 Telegram 支持的格式

        Telegram Markdown 限制：
        - 不支持 # 标题（转为 *bold*）
        - 使用 *bold* 而非 **bold**
        - 不支持管道表格（手机端比例字体会错位，转为「键：值」行）

        委托 formatters.format_telegram_markdown 统一处理：
        表格 → 键值行、标题 → 加粗、分隔线 → 长横线、代码块保护、
        非链接方括号/圆括号转义。
        Telegram 正文另会剔除阶段/数据质量等内部调试行（仅影响本渠道）。
        """
        result = strip_hidden_markdown_metadata(text)
        result = self._drop_internal_metadata_lines(result)
        return format_telegram_markdown(result)
