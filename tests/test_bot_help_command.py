# -*- coding: utf-8 -*-
"""Tests for the /help command output.

帮助内容必须全部来自 Command Registry（command / aliases / description /
usage / examples），不能硬编码、不能出现 Markdown 转义乱码（反斜杠泄漏）。
"""

import sys
import unittest

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from bot.commands.help import HelpCommand
from bot.commands.start import StartCommand
from bot.dispatcher import get_dispatcher, reset_dispatcher
from bot.models import BotMessage, BotResponse, ChatType


def _make_message(content: str) -> BotMessage:
    return BotMessage(
        platform="telegram",
        message_id="m1",
        user_id="u1",
        user_name="tester",
        chat_id="c1",
        chat_type=ChatType.PRIVATE,
        content=content,
        raw_content=content,
        mentioned=False,
        timestamp=1700000000,
    )


class TestHelpCommand(unittest.TestCase):
    def setUp(self):
        reset_dispatcher()
        self.dispatcher = get_dispatcher()
        self.help_cmd = HelpCommand()

    def tearDown(self):
        reset_dispatcher()

    def test_help_list_groups_commands_and_excludes_help_itself(self):
        """主列表不包含 /help 自身与隐藏命令，且展示真实注册命令。"""
        commands = self.dispatcher.list_commands(include_hidden=False)
        text = self.help_cmd._format_help_list(commands, "/")

        self.assertIn("📚 **股票分析助手**", text)
        # 隐藏命令 /start 不出现
        self.assertNotIn("/start", text)
        # /help 自身不重复出现在分组里
        self.assertNotIn("/help —", text)
        # 低频命令仍通过 /help 可达
        self.assertIn("/history", text)
        self.assertIn("/strategies", text)
        # 常见命令按分组展示
        self.assertIn("📊 股票分析", text)
        self.assertIn("🤖 AI 功能", text)
        self.assertIn("/analyze", text)
        self.assertIn("/ask", text)
        # 引导文案
        self.assertIn("发送 /help <命令名> 查看某个命令的详细说明", text)

    def test_help_list_has_no_backslash_leak(self):
        """主列表不得出现反斜杠转义乱码。"""
        commands = self.dispatcher.list_commands(include_hidden=False)
        text = self.help_cmd._format_help_list(commands, "/")
        self.assertNotIn("\\", text)

    def test_help_detail_ask_keeps_brackets_without_backslash(self):
        # /help ask 不得把 [技能名称] 转义成 \[技能名称\]。
        command = self.dispatcher.get_command("ask")
        text = self.help_cmd._format_command_help(command, "/")

        self.assertIn("/ask <股票代码[,代码2,...]> [技能名称]", text)
        self.assertNotIn("\\", text)

    def test_help_detail_all_commands_have_no_backslash_leak(self):
        # 逐个检查 /help <command> 的输出，确保没有反斜杠乱码。
        for name in ("analyze", "ask", "batch", "chat", "market", "research",
                     "status", "strategies", "history", "help"):
            command = self.dispatcher.get_command(name)
            self.assertIsNotNone(command, name)
            text = self.help_cmd._format_command_help(command, "/")
            self.assertNotIn("\\", text, f"/help {name} 出现反斜杠: {text!r}")
            # 用法行总是存在
            self.assertIn(f"用法：", text)

    def test_help_detail_includes_examples_and_english_aliases(self):
        command = self.dispatcher.get_command("analyze")
        text = self.help_cmd._format_command_help(command, "/")
        self.assertIn("/analyze 600519", text)
        self.assertIn("/analyze 600519 full", text)
        self.assertIn("别名：/a", text)

    def test_help_dispatch_returns_list_for_no_args(self):
        response = self.help_cmd.execute(_make_message("/help"), [])
        self.assertIsInstance(response, BotResponse)
        self.assertIn("📚 **股票分析助手**", response.text)
        self.assertNotIn("\\", response.text)

    def test_help_dispatch_detail_returns_single_command(self):
        response = self.help_cmd.execute(_make_message("/help ask"), ["ask"])
        self.assertIsInstance(response, BotResponse)
        self.assertIn("/ask <股票代码[,代码2,...]> [技能名称]", response.text)
        self.assertNotIn("\\", response.text)


if __name__ == "__main__":
    unittest.main()
