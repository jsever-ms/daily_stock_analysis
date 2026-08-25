# -*- coding: utf-8 -*-
"""
独立 Telegram 快速测试脚本（不运行股票分析流程，最小依赖）

目标：5~10 秒内验证 GitHub Secrets 中 Telegram 配置是否可用。
本脚本只依赖标准库 + `requests` + `markdown2`，不会通过导入链连带加载
EmailSender / Feishu / data_provider / 股票行情等无关模块：

- 通过 ``importlib`` 直接按文件路径加载 ``telegram_sender.py`` 与 ``runtime_info.py``，
  刻意跳过 ``src.notification_sender/__init__.py``（会导入全部发送器）与
  ``bot/__init__.py``（会导入 dispatcher → src.config 重型链路）的包级导入；
- 不导入 ``src.config``，但 Token / Chat ID 读取的环境变量名与正式代码
  （``src/config.py`` 的 ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``）完全一致，
  即同一份 GitHub Secrets。

流程：
    1. 读取 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID（与正式代码同源）
    2. getMe 验证 Token
    3. 成功后 sendMessage（复用正式 TelegramSender 同一条发送逻辑）
    4. Telegram 收到："✅ Telegram 推送测试成功"（含时间、运行环境、git commit）

安全要求：
    - 禁止打印完整 Bot Token，仅打印存在性 / 长度 / 空格 / 换行 / 冒号
    - 明确区分 401 / 400 / 403 / 404 / 网络错误
    - 日志先出现 getMe 状态，再出现 sendMessage 状态

退出码：0 = getMe + sendMessage 全部成功；1 = 失败（缺少配置 / Token 无效 / 发送失败）

用法：
    python scripts/test_telegram.py
"""

import importlib.util
import logging
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("test_telegram")


def _load_module_from_file(module_name: str, relative_path: str):
    """按文件路径加载模块，避免触发其所在包的 __init__ 连带导入。

    仅用于加载真正轻量的模块（telegram_sender.py / runtime_info.py）。
    """
    file_path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块文件: {file_path}")
    module = importlib.util.module_from_spec(spec)
    # 让模块内部的 `from src.formatters import ...` 等正常解析
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _read_telegram_config() -> tuple:
    """读取 Token / Chat ID，环境变量名与正式代码 src/config.py 完全一致。"""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "") or ""
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "") or ""
    return bot_token, chat_id


def _describe_status(kind: str, status) -> str:
    """将 HTTP 状态映射为明确的诊断结论。status 为 None 表示网络层失败。"""
    if status is None:
        return f"{kind}: 网络错误（无法连接到 Telegram API）"
    if status == 200:
        return f"{kind}: 成功 (HTTP 200)"
    if status == 401:
        return f"{kind}: Token 无效或未正确加载 (HTTP 401)"
    if status == 400:
        return f"{kind}: Chat ID / 请求参数问题 (HTTP 400)"
    if status == 403:
        return f"{kind}: Bot 被阻止或无权限 (HTTP 403)"
    if status == 404:
        return f"{kind}: URL / Token 格式异常 (HTTP 404)"
    if status == 429:
        return f"{kind}: 触发频率限制 (HTTP 429)"
    return f"{kind}: 非预期状态 (HTTP {status})"


def _load_sender():
    """加载正式 TelegramSender（仅其所在文件，跳过包 __init__）。"""
    sender_module = _load_module_from_file("_tg_test_telegram_sender",
                                           "src/notification_sender/telegram_sender.py")
    return sender_module.TelegramSender


def _build_test_message() -> str:
    """构造测试消息：固定文案 + 时间 / 运行环境 / git commit。"""
    revision = "unknown"
    try:
        runtime_info = _load_module_from_file("_tg_test_runtime_info", "bot/runtime_info.py")
        revision = runtime_info.get_runtime_revision()
    except Exception:
        revision = os.getenv("DSA_GIT_COMMIT", "").strip() or "unknown"

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    env = f"{platform.system()} / Python {platform.python_version()}"
    return (
        "✅ Telegram 推送测试成功\n"
        f"\n"
        f"时间: {now}\n"
        f"运行环境: {env}\n"
        f"git commit: {revision}"
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )

    bot_token, chat_id = _read_telegram_config()

    # --- 安全诊断：只输出概要，禁止完整 Token ---
    has_space = " " in bot_token
    has_newline = any(c in bot_token for c in ("\n", "\r"))
    has_colon = ":" in bot_token
    print("==========================================")
    print("Telegram 推送快速测试")
    print("==========================================")
    print(f"Telegram token: present={bool(bot_token)}, len={len(bot_token)}, "
          f"space={has_space}, newline={has_newline}, colon={has_colon}")
    print(f"Telegram chat_id: present={bool(chat_id)}")

    if not bot_token:
        print("❌ TELEGRAM_BOT_TOKEN 未配置或为空，请检查 GitHub Secret / 环境变量。")
        return 1
    if has_space or has_newline:
        print("❌ Token 疑似包含多余空格/换行（Secret 粘贴事故），"
              "请编辑 Secret 全选清空后重新粘贴。")
        return 1

    # --- 复用正式 TelegramSender（同一底层发送逻辑 + 同一配置属性名） ---
    TelegramSender = _load_sender()
    sender = TelegramSender(SimpleNamespace(
        telegram_bot_token=bot_token,
        telegram_chat_id=chat_id,
        telegram_message_thread_id=None,
    ))

    # --- 1. getMe 验证（先输出 getMe 状态） ---
    get_me_ok = sender.verify_token()
    print(_describe_status("getMe", sender.last_get_me_status))

    if sender.last_get_me_status == 401:
        print("❌ Token 无效，请确认 BotFather 最新 Token 已更新到 GitHub Secret 且未被 revoke。")
        return 1

    if not chat_id:
        print("❌ TELEGRAM_CHAT_ID 未配置或为空，请检查 GitHub Secret / 环境变量。")
        print("   获取方式：私聊 @userinfobot 或向机器人发消息后查 getUpdates。")
        return 1

    if not get_me_ok:
        # 非 401 的 getMe 异常（如网络错误），仍尝试发送以暴露 sendMessage 状态
        print("⚠️ getMe 未明确成功，继续尝试 sendMessage 以确认发送链路。")

    # --- 2. sendMessage（复用正式 TelegramSender 同一条发送逻辑） ---
    message = _build_test_message()
    send_ok = sender.send_to_telegram(message)
    print(_describe_status("sendMessage", sender.last_send_message_status))

    if send_ok:
        print("✅ Telegram 推送测试成功，请查收消息。")
        return 0
    print("❌ Telegram 推送测试失败，请根据上方状态码修复配置。")
    return 1


if __name__ == "__main__":
    sys.exit(main())