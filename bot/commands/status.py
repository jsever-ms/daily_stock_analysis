# -*- coding: utf-8 -*-
"""
===================================
状态命令
===================================

显示系统运行状态和配置信息。
"""

import platform
import sys
from datetime import datetime
from typing import List

from bot.commands.base import BotCommand
from bot.models import BotMessage, BotResponse


class StatusCommand(BotCommand):
    """
    状态命令
    
    显示系统运行状态，包括：
    - 服务状态
    - 配置信息
    - 可用功能
    """
    
    @property
    def name(self) -> str:
        return "status"
    
    @property
    def aliases(self) -> List[str]:
        return ["s", "状态", "info"]
    
    @property
    def description(self) -> str:
        return "查看机器人、AI、数据源状态"

    @property
    def usage(self) -> str:
        return "/status [detail]"

    @property
    def menu_label(self) -> str:
        return "🟢 系统状态"
    
    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行状态命令"""
        from src.config import get_config

        config = get_config()

        # 收集状态信息
        status_info = self._collect_status(config)

        # 默认精简输出，/status detail 才输出完整诊断
        detailed = bool(args and args[0].lower() in ("detail", "full", "详细", "完整"))
        if detailed:
            text = self._format_status_detail(status_info, message.platform)
        else:
            text = self._format_status_summary(status_info)

        return BotResponse.markdown_response(text)
    
    def _collect_status(self, config) -> dict:
        """收集系统状态信息"""
        from src.config import _uses_direct_env_provider, get_configured_llm_models

        status = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": platform.system(),
            "stock_count": len(config.stock_list),
            "stock_list": config.stock_list[:5],  # 只显示前5个
        }

        # 运行版本与命令注册信息：用于实机直接核对部署进程加载的代码版本，
        # 排查“代码已更新但进程未重启 / 镜像未重建”的版本漂移问题。
        status["runtime_revision"] = self._collect_runtime_revision()
        status["runtime_commands"], status["runtime_command_count"] = self._collect_registered_commands()
        status["telegram_polling_running"] = self._collect_telegram_polling_state()

        # 运行版本诊断：进程 PID / 启动时间 / 部署环境，用于确认"实际处理消息的进程"
        import os as _os
        status["process_pid"] = _os.getpid()
        try:
            from bot.runtime_info import get_deployment_env, get_process_startup_time
            status["deployment_env"] = get_deployment_env()
            status["startup_time"] = get_process_startup_time()
        except Exception as exc:  # pragma: no cover - 防御性兜底
            status["deployment_env"] = "unknown"
            status["startup_time"] = f"获取失败: {exc}"
        
        # AI 配置状态
        llm_channels = getattr(config, "llm_channels", []) or []
        llm_model_list = getattr(config, "llm_model_list", []) or []
        llm_model = (getattr(config, "litellm_model", "") or "").strip()
        agent_model = (getattr(config, "agent_litellm_model", "") or "").strip()
        ask_fast_model = (getattr(config, "ask_fast_model", "") or "").strip()
        status["ai_primary_model"] = llm_model
        status["ai_agent_model"] = agent_model or ("继承主模型" if llm_model else "")
        status["ai_ask_fast_model"] = ask_fast_model or ("继承主模型" if llm_model else "")
        status["ai_channels"] = [
            str(channel.get("name") or "").strip()
            for channel in llm_channels
            if str(channel.get("name") or "").strip()
        ]
        status["ai_yaml"] = (
            getattr(config, "llm_models_source", "") == "litellm_config"
            and bool(llm_model_list)
        )
        status["ai_legacy_keys"] = {
            "Gemini": bool(getattr(config, "gemini_api_keys", [])),
            "OpenAI": bool(getattr(config, "openai_api_keys", [])),
            "Anthropic": bool(getattr(config, "anthropic_api_keys", [])),
            "DeepSeek": bool(getattr(config, "deepseek_api_keys", [])),
        }
        has_direct_env_model = bool(llm_model) and _uses_direct_env_provider(llm_model)
        available_router_model_set = set(get_configured_llm_models(llm_model_list))
        primary_model_reachable = not (
            available_router_model_set
            and llm_model
            and not _uses_direct_env_provider(llm_model)
            and llm_model not in available_router_model_set
        )
        status["ai_available"] = bool(
            llm_model
            and (has_direct_env_model or (llm_model_list and primary_model_reachable))
        )
        
        # 搜索服务状态
        status["search_bocha"] = len(config.bocha_api_keys) > 0
        status["search_tavily"] = len(config.tavily_api_keys) > 0
        status["search_brave"] = len(config.brave_api_keys) > 0
        status["search_serpapi"] = len(config.serpapi_keys) > 0
        status["search_minimax"] = len(config.minimax_api_keys) > 0
        status["search_searxng"] = config.has_searxng_enabled()
        status["search_anspire"] = len(config.anspire_api_keys) > 0

        # 行情数据：数据源优先级非空即视为已配置（免费源无需 key，腾讯/新浪默认可用）
        market_priority = (getattr(config, "realtime_source_priority", "") or "").strip()
        status["market_data_available"] = bool(market_priority)
        status["market_data_sources"] = [s.strip() for s in market_priority.split(",") if s.strip()]
        
        # 通知渠道状态
        status["notify_wechat"] = bool(config.wechat_webhook_url)
        status["notify_feishu"] = bool(config.feishu_webhook_url)
        status["notify_telegram"] = bool(config.telegram_bot_token and config.telegram_chat_id)
        status["notify_email"] = bool(config.email_sender and config.email_password)
        status["notify_custom"] = bool(getattr(config, "custom_webhook_urls", []))
        status["notify_discord"] = bool(
            getattr(config, "discord_webhook_url", None)
            or (
                getattr(config, "discord_bot_token", None)
                and getattr(config, "discord_main_channel_id", None)
            )
        )
        status["notify_slack"] = bool(
            getattr(config, "slack_webhook_url", None)
            or (
                getattr(config, "slack_bot_token", None)
                and getattr(config, "slack_channel_id", None)
            )
        )
        status["notify_push"] = bool(
            getattr(config, "pushplus_token", None)
            or (
                getattr(config, "pushover_user_key", None)
                and getattr(config, "pushover_api_token", None)
            )
            or getattr(config, "serverchan3_sendkey", None)
        )
        
        return status
    
    @staticmethod
    def _collect_runtime_revision() -> str:
        """当前进程加载的代码版本（git revision）。"""
        try:
            from bot.runtime_info import get_runtime_revision
            return get_runtime_revision()
        except Exception as exc:
            return f"获取失败: {exc}"

    @staticmethod
    def _collect_registered_commands() -> tuple:
        """当前 dispatcher 实例真实注册的命令集合。"""
        try:
            from bot.dispatcher import get_dispatcher
            commands = get_dispatcher().list_commands(include_hidden=True)
            names = sorted(c.name for c in commands)
            return ", ".join(names), len(names)
        except Exception as exc:
            return f"获取失败: {exc}", 0

    @staticmethod
    def _collect_telegram_polling_state() -> bool:
        """Telegram 轮询客户端是否在当前进程内运行。"""
        try:
            from bot.platforms.telegram_polling import _polling_client
            return bool(_polling_client and _polling_client.is_running)
        except Exception:
            return False

    def _format_status_summary(self, status: dict) -> str:
        """精简状态（默认）：只显示手机端最关心的几行。

        完整的命令注册、通知渠道、搜索服务明细等放到 ``/status detail``。
        """
        def ok(enabled: bool) -> str:
            return "✅" if enabled else "❌"

        # 主模型名称（精简 provider 前缀）
        primary_model = str(status.get("ai_primary_model") or "").strip() or "未配置"
        # Agent 模型名称
        agent_model = str(status.get("ai_agent_model") or "").strip() or "未配置"

        # 新闻搜索：任一搜索源可用即为 ✅
        search_any = any(
            status.get(key)
            for key in (
                "search_bocha", "search_tavily", "search_brave",
                "search_serpapi", "search_minimax", "search_searxng",
                "search_anspire",
            )
        )

        telegram_status = "✅ 在线" if status.get("telegram_polling_running") else "❌ 离线"

        lines = [
            "🟢 **系统状态**",
            "",
            f"Telegram：{telegram_status}",
            f"主模型：{primary_model}",
            f"Agent模型：{agent_model}",
            f"行情数据：{ok(status.get('market_data_available'))}",
            f"新闻搜索：{ok(search_any)}",
            f"自选股：{status['stock_count']} 只",
            f"代码版本：{status.get('runtime_revision', 'unknown')}",
            "",
            "发送 /status detail 查看详细诊断",
        ]

        return "\n".join(lines)

    def _format_status_detail(self, status: dict, platform: str) -> str:
        """完整诊断（/status detail）：保留全部维度明细。"""
        # 状态图标
        def icon(enabled: bool) -> str:
            return "✅" if enabled else "❌"
        
        lines = [
            "📊 **股票分析助手 - 系统状态**",
            "",
            f"🕐 时间: {status['timestamp']}",
            f"🐍 Python: {status['python_version']}",
            f"💻 平台: {status['platform']}",
            "",
            "**🧭 运行信息**",
            f"• 代码版本: {status.get('runtime_revision', 'unknown')}",
            f"• 进程 PID: {status.get('process_pid', '-')}",
            f"• 启动时间: {status.get('startup_time', 'unknown')}",
            f"• 部署环境: {status.get('deployment_env', 'unknown')}",
            f"• 已注册命令({status.get('runtime_command_count', 0)}): {status.get('runtime_commands', '-')}",
            f"• Telegram 轮询: {'✅ 运行中' if status.get('telegram_polling_running') else '❌ 未运行'}",
            "",
            "---",
            "",
            "**📈 自选股配置**",
            f"• 股票数量: {status['stock_count']} 只",
        ]
        
        if status['stock_list']:
            stocks_preview = ", ".join(status['stock_list'])
            if status['stock_count'] > 5:
                stocks_preview += f" ... 等 {status['stock_count']} 只"
            lines.append(f"• 股票列表: {stocks_preview}")
        
        lines.extend([
            "",
            "**🤖 AI 分析服务**",
            f"• 主模型: {status['ai_primary_model'] or '未配置'}",
            f"• Agent 模型: {status['ai_agent_model'] or '未配置'}",
            f"• 快速问股模型: {status['ai_ask_fast_model'] or '未配置'}",
            f"• LLM 渠道: {', '.join(status['ai_channels']) if status['ai_channels'] else '未配置'}",
            f"• LiteLLM YAML: {icon(status['ai_yaml'])}",
            "• Legacy Key: "
            + ", ".join(
                f"{name}{icon(enabled)}"
                for name, enabled in status["ai_legacy_keys"].items()
            ),
            "",
            "**🔍 搜索服务**",
            f"• Bocha: {icon(status['search_bocha'])}",
            f"• Tavily: {icon(status['search_tavily'])}",
            f"• Brave: {icon(status['search_brave'])}",
            f"• SerpAPI: {icon(status['search_serpapi'])}",
            f"• MiniMax: {icon(status['search_minimax'])}",
            f"• SearXNG: {icon(status['search_searxng'])}",
            f"• Anspire: {icon(status['search_anspire'])}",
            "",
            "**📢 通知渠道**",
            f"• 企业微信: {icon(status['notify_wechat'])}",
            f"• 飞书: {icon(status['notify_feishu'])}",
            f"• Telegram: {icon(status['notify_telegram'])}",
            f"• 邮件: {icon(status['notify_email'])}",
            f"• 自定义 Webhook: {icon(status['notify_custom'])}",
            f"• Discord: {icon(status['notify_discord'])}",
            f"• Slack: {icon(status['notify_slack'])}",
            f"• PushPlus/Pushover/Server酱3: {icon(status['notify_push'])}",
        ])
        
        # AI 服务总体状态
        if status["ai_available"]:
            lines.extend([
                "",
                "---",
                "✅ **系统就绪，可以开始分析！**",
            ])
        else:
            lines.extend([
                "",
                "---",
                "⚠️ **AI 服务未配置，分析功能不可用**",
                "请配置 LITELLM_MODEL、LLM_CHANNELS、LITELLM_CONFIG 或任一 provider API Key",
            ])
        
        return "\n".join(lines)
