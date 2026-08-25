# -*- coding: utf-8 -*-
"""
===================================
帮助命令
===================================

显示可用命令列表和使用说明。
"""

from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse


class HelpCommand(BotCommand):
    """
    帮助命令
    
    显示所有可用命令的列表和使用说明。
    也可以查看特定命令的详细帮助。
    
    用法：
        /help         - 显示所有命令
        /help analyze - 显示 analyze 命令的详细帮助
    """
    
    @property
    def name(self) -> str:
        return "help"
    
    @property
    def aliases(self) -> List[str]:
        return ["h", "帮助", "?"]
    
    @property
    def description(self) -> str:
        return "使用帮助"

    @property
    def usage(self) -> str:
        return "/help [命令名]"

    @property
    def menu_label(self) -> str:
        return "❓ 使用帮助"
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行帮助命令"""
        # 延迟导入避免循环依赖
        from bot.dispatcher import get_dispatcher

        dispatcher = get_dispatcher()

        # 如果指定了命令名，显示该命令的详细帮助
        if args:
            cmd_name = args[0]
            command = dispatcher.get_command(cmd_name)

            if command is None:
                return BotResponse.error_response(f"未知命令: {cmd_name}")

            # 构建详细帮助
            help_text = self._format_command_help(command, dispatcher.command_prefix)
            return BotResponse.markdown_response(help_text)

        # 显示所有命令列表
        commands = dispatcher.list_commands(include_hidden=False)
        prefix = dispatcher.command_prefix

        help_text = self._format_help_list(commands, prefix)
        return BotResponse.markdown_response(help_text)

    def _format_help_list(self, commands: List[BotCommand], prefix: str) -> str:
        """手机友好的命令列表：按分组展示，命令带一条快捷示例。

        全部数据（命令集合、描述、示例）来自 Command Registry，
        不在这里硬编码任何命令，避免新增命令后帮助信息过期。
        """
        from bot.commands.base import (
            CATEGORY_STOCK,
            CATEGORY_AI,
            CATEGORY_OTHER,
            CATEGORY_EMOJI,
        )

        # /help 自身作为入口命令，在列表底部引导，不重复出现在分组里
        visible = [c for c in commands if c.name != "help"]

        # 分组顺序：股票分析 -> AI 功能 -> 其他
        ordered_categories = [CATEGORY_STOCK, CATEGORY_AI, CATEGORY_OTHER]
        grouped: dict = {}
        for cmd in visible:
            grouped.setdefault(cmd.category, []).append(cmd)

        lines = ["📚 **股票分析助手**", ""]

        for category in ordered_categories:
            cat_commands = grouped.get(category)
            if not cat_commands:
                continue
            emoji = CATEGORY_EMOJI.get(category, "📌")
            lines.append(f"{emoji} {category}")
            for cmd in cat_commands:
                # 主列表取第一条示例（含参数），例如 "/analyze 600519"；
                # 无示例的命令退化为 "命令名" 本身。
                display = cmd.examples[0] if cmd.examples else f"{prefix}{cmd.name}"
                lines.append(f"{display} — {cmd.description}")
            lines.append("")

        lines.append(f"发送 {prefix}help <命令名> 查看某个命令的详细说明")

        return "\n".join(lines).strip()

    def _format_command_help(self, command: BotCommand, prefix: str) -> str:
        """格式化单个命令的详细帮助。

        内容全部来自 Command Registry（command / aliases / description /
        usage / examples），避免 Markdown 源码泄漏与手工复制过期文案。
        """
        lines = [
            f"📖 {prefix}{command.name} — {command.description}",
            "",
            f"用法：{command.usage}",
        ]

        # 示例（可选）
        if command.examples:
            lines.append("")
            lines.append("示例：")
            for example in command.examples:
                lines.append(example)

        # 别名：只展示英文别名，避免中文别名在手机端与命令前缀拼接产生歧义
        en_aliases = [a for a in command.aliases if a.isascii()]
        if en_aliases:
            lines.append("")
            lines.append("别名：" + "、".join(f"{prefix}{a}" for a in en_aliases))

        # 权限
        if command.admin_only:
            lines.append("")
            lines.append("⚠️ 需要管理员权限")

        return "\n".join(lines)
