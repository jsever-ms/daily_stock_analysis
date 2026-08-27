# -*- coding: utf-8 -*-
"""
===================================
开始命令
===================================

Telegram 等平台的标准入口命令，展示机器人能力概览并引导查看帮助。
"""

from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse


class StartCommand(BotCommand):
    """
    开始命令

    作为 Telegram 私聊 "Start" 按钮等入口的响应，引导用户使用机器人。
    """

    @property
    def name(self) -> str:
        return "start"

    @property
    def aliases(self) -> List[str]:
        return ["开始"]

    @property
    def description(self) -> str:
        return "开始使用机器人"

    @property
    def usage(self) -> str:
        return "/start"

    @property
    def hidden(self) -> bool:
        """/start 是平台入口命令，不显示在 /help 列表与 Telegram 菜单中。"""
        return True

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行开始命令"""
        return BotResponse.markdown_response(
            "👋 **欢迎使用股票分析助手！**\n\n"
            "我可以帮你：\n"
            "• 快速问股：`/ask 600519`\n"
            "• 后台深度分析：`/analyze 600519`\n"
            "• 批量分析自选股：`/batch`\n"
            "• 大盘复盘：`/market`\n"
            "• 深度研究：`/research 600519`\n"
            "• 自由对话：`/chat 你的问题`\n\n"
            "发送 `/help` 查看全部可用命令。"
        )
