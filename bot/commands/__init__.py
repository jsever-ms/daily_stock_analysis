# -*- coding: utf-8 -*-
"""
===================================
命令处理器模块
===================================

包含所有机器人命令的实现。
"""

from bot.commands.base import BotCommand
from bot.commands.start import StartCommand
from bot.commands.help import HelpCommand
from bot.commands.status import StatusCommand
from bot.commands.analyze import AnalyzeCommand
from bot.commands.market import MarketCommand
from bot.commands.batch import BatchCommand
from bot.commands.ask import AskCommand
from bot.commands.chat import ChatCommand
from bot.commands.research import ResearchCommand
from bot.commands.strategies import StrategiesCommand
from bot.commands.history import HistoryCommand

# All available commands (for auto-registration)
ALL_COMMANDS = [
    StartCommand,
    HelpCommand,
    StatusCommand,
    AnalyzeCommand,
    MarketCommand,
    BatchCommand,
    AskCommand,
    ChatCommand,
    ResearchCommand,
    StrategiesCommand,
    HistoryCommand,
]

__all__ = [
    'BotCommand',
    'StartCommand',
    'HelpCommand',
    'StatusCommand',
    'AnalyzeCommand',
    'MarketCommand',
    'BatchCommand',
    'AskCommand',
    'ChatCommand',
    'ResearchCommand',
    'StrategiesCommand',
    'HistoryCommand',
    'ALL_COMMANDS',
]
