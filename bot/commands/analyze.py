# -*- coding: utf-8 -*-
"""
===================================
股票分析命令
===================================

分析指定股票，调用 AI 生成分析报告。

默认 /analyze <code> 和 /analyze <code> full 都走详细/完整分析。
显式传入 brief 可切换到精简模式：/analyze <code> brief。
"""

import re
import logging
from typing import List, Optional

from bot.commands.base import BotCommand, CATEGORY_STOCK
from bot.models import BotMessage, BotResponse
from src.services.stock_code_utils import resolve_index_stock_code_for_analysis
from src.services.stock_resolver import resolve_stock_label

logger = logging.getLogger(__name__)


class AnalyzeCommand(BotCommand):
    """
    股票分析命令

    分析指定股票代码，生成 AI 分析报告并推送。

    默认 /analyze <code> 和 /analyze <code> full 都走详细/完整分析。
    显式传入 brief 可切换到精简模式：/analyze <code> brief。
    """

    @property
    def name(self) -> str:
        return "analyze"

    @property
    def aliases(self) -> List[str]:
        return ["a", "分析", "查"]

    @property
    def description(self) -> str:
        return "后台深度分析一只股票"

    @property
    def usage(self) -> str:
        return "/analyze <股票代码> [brief]"

    @property
    def examples(self) -> List[str]:
        return ["/analyze 600519", "/analyze 600519 brief"]

    @property
    def category(self) -> str:
        return CATEGORY_STOCK

    @property
    def menu_label(self) -> str:
        return "📊 分析单只股票"

    def validate_args(self, args: List[str]) -> Optional[str]:
        """验证参数"""
        if not args:
            return "请输入股票代码"

        code = args[0].upper()

        # 验证股票代码格式
        # A股：6位数字
        # 港股：HK+5位数字
        # 美股：1-5个大写字母+.+2个后缀字母
        is_a_stock = re.match(r'^\d{6}$', code)
        is_hk_stock = re.match(r'^HK\d{5}$', code)
        is_us_stock = re.match(r'^[A-Z]{1,5}(\.[A-Z]{1,2})?$', code)

        if not (is_a_stock or is_hk_stock or is_us_stock):
            return f"无效的股票代码: {code}（A股6位数字 / 港股HK+5位数字 / 美股1-5个字母）"

        return None

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """执行分析命令"""
        code = resolve_index_stock_code_for_analysis(args[0])

        # 默认详细模式；显式 brief 走精简
        is_brief = len(args) > 1 and args[1].lower() in ("brief", "简单", "精简")
        report_type = "brief" if is_brief else "full"

        # 解析股票名称
        stock_label = resolve_stock_label(code)

        logger.info(f"[AnalyzeCommand] 分析股票: {stock_label}, 报告类型: {report_type}")

        try:
            # 调用分析服务
            from src.services.task_service import get_task_service
            from src.enums import ReportType

            service = get_task_service()

            # 提交异步分析任务
            result = service.submit_analysis(
                code=code,
                report_type=ReportType.from_str(report_type),
                source_message=message
            )

            if result.get("success"):
                task_id = result.get("task_id", "")

                if is_brief:
                    return BotResponse.markdown_response(
                        f"✅ **分析任务已提交**\n\n"
                        f"📊 股票：{stock_label}\n"
                        f"📑 报告类型：精简报告\n"
                        f"⏳ 精简分析步骤较少，将较快完成\n\n"
                        f"分析完成后机器人会自动推送，无需重复提交。"
                    )

                return BotResponse.markdown_response(
                    f"✅ **深度分析任务已提交**\n\n"
                    f"📊 股票：{stock_label}\n"
                    f"📑 报告类型：详细报告\n"
                    f"🔍 将分析：行情、技术面、基本面、资金、新闻/公告、AI综合研判\n"
                    f"⏳ 详细分析步骤较多，将在后台执行\n\n"
                    f"完成后机器人会自动推送完整报告，无需重复提交。"
                )
            else:
                error = result.get("error", "未知错误")
                return BotResponse.error_response(f"提交分析任务失败: {error}")

        except Exception as e:
            logger.error(f"[AnalyzeCommand] 执行失败: {e}")
            return BotResponse.error_response(f"分析失败: {str(e)[:100]}")
