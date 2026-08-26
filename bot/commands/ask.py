# -*- coding: utf-8 -*-
"""
Ask command - analyze one or more stocks using Agent skills.

Usage:
    /ask 600519                        -> Analyze with default skill
    /ask 600519 用缠论分析              -> Parse skill from message
    /ask 600519 chan_theory             -> Specify skill id directly
    /ask 600519,000858 波浪理论         -> Multi-stock comparison with skill overlay
"""

import logging
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from bot.commands.base import BotCommand, CATEGORY_AI
from bot.models import BotMessage, BotResponse
from data_provider.base import canonical_stock_code
from src.config import get_config
from src.storage import get_db

logger = logging.getLogger(__name__)


class AskCommand(BotCommand):
    """
    Ask command handler - invoke Agent with a specific skill to analyze stocks.
    """

    _MULTI_ANALYZE_TIMEOUT_S = 150.0

    @property
    def name(self) -> str:
        return "ask"

    @property
    def aliases(self) -> List[str]:
        return ["问股"]

    @property
    def description(self) -> str:
        return "AI Agent 智能问股"

    @property
    def usage(self) -> str:
        return "/ask <股票代码[,代码2,...]> [技能名称]"

    @property
    def examples(self) -> List[str]:
        return ["/ask 600519", "/ask 600519,000858 缠论"]

    @property
    def category(self) -> str:
        return CATEGORY_AI

    @property
    def menu_label(self) -> str:
        return "🤖 AI 智能问股"

    def _merge_code_args(self, args: List[str]) -> tuple[str, List[str]]:
        """Merge stock code arguments separated by commas or explicit ``vs`` markers."""
        if not args:
            return "", []

        code_like = re.compile(
            r"^,?(\d{6}|hk\d{5}|[A-Za-z]{1,5}(\.[A-Za-z]{1,2})?),?$",
            re.IGNORECASE,
        )
        raw_codes_parts = [args[0]]
        rest_args = list(args[1:])

        while rest_args:
            token = rest_args[0]
            prev = raw_codes_parts[-1].rstrip()

            if token.lower() == "vs" and len(rest_args) > 1 and code_like.match(rest_args[1]):
                raw_codes_parts.append(rest_args[1])
                rest_args = rest_args[2:]
                continue

            has_comma_separator = (
                prev.endswith(",")
                or prev.endswith("，")
                or token.lstrip().startswith(",")
                or token.lstrip().startswith("，")
            )
            if code_like.match(token) and has_comma_separator:
                raw_codes_parts.append(token)
                rest_args = rest_args[1:]
                continue

            break

        normalized_parts = [part.strip(",，") for part in raw_codes_parts]
        raw_code_str = ",".join(normalized_parts)
        return raw_code_str, rest_args

    def _parse_stock_codes(self, raw: str) -> List[str]:
        """Parse one or more stock codes from the first argument."""
        parts = [p.strip().upper() for p in raw.replace("，", ",").split(",") if p.strip()]
        return [canonical_stock_code(part) for part in parts]

    def _validate_single_code(self, code: str) -> Optional[str]:
        """Validate a single stock code format."""
        normalized = code.upper()
        is_a_stock = re.match(r"^\d{6}$", normalized)
        is_hk_stock = re.match(r"^HK\d{5}$", normalized)
        is_us_stock = re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", normalized)

        if not (is_a_stock or is_hk_stock or is_us_stock):
            return f"无效的股票代码: {normalized}（A股6位数字 / 港股HK+5位数字 / 美股1-5个字母）"
        return None

    def validate_args(self, args: List[str]) -> Optional[str]:
        """Validate arguments."""
        if not args:
            return "请输入股票代码。用法: /ask <股票代码[,代码2,...]> [技能名称]"

        raw_code_str, _ = self._merge_code_args(args)
        codes = self._parse_stock_codes(raw_code_str)
        if not codes:
            return "请输入至少一个有效的股票代码"

        for code in codes:
            error = self._validate_single_code(code)
            if error:
                return error

        if len(codes) > 5:
            return "一次最多分析 5 只股票"

        return None

    @staticmethod
    def _load_skills() -> List[object]:
        try:
            from src.agent.factory import get_skill_manager

            sm = get_skill_manager()
            return list(sm.list_skills())
        except Exception as e:
            logger.warning("_load_skills failed: Failed to load skills: %s", e, exc_info=True)
            return []

    @classmethod
    def _get_default_skill_id(cls) -> str:
        try:
            from src.agent.skills.defaults import get_primary_default_skill_id

            return get_primary_default_skill_id(cls._load_skills())
        except Exception as e:
            logger.warning("_get_default_skill_id failed: Failed to resolve default skill id: %s", e, exc_info=True)
            return ""

    @classmethod
    def _build_skill_alias_pairs(cls) -> List[tuple[str, str]]:
        alias_pairs: List[tuple[str, str]] = []
        for skill in cls._load_skills():
            skill_id = str(getattr(skill, "name", "")).strip()
            if not skill_id:
                continue
            aliases = [skill_id, getattr(skill, "display_name", "")] + list(getattr(skill, "aliases", []) or [])
            for alias in aliases:
                alias_text = str(alias).strip()
                if alias_text:
                    alias_pairs.append((alias_text, skill_id))

        alias_pairs.sort(key=lambda item: (len(item[0]), item[0]), reverse=True)
        return alias_pairs

    def _parse_skill(self, args: List[str]) -> str:
        """Parse skill from arguments, returning the resolved skill id."""
        default_skill_id = self._get_default_skill_id()
        if len(args) < 2:
            return default_skill_id

        skill_text = " ".join(args[1:]).strip()
        available_ids = {str(getattr(skill, "name", "")).strip() for skill in self._load_skills()}
        if skill_text in available_ids:
            return skill_text

        for alias_text, skill_id in self._build_skill_alias_pairs():
            if alias_text in skill_text:
                return skill_id

        return default_skill_id

    def _resolve_skill_name(self, skill_id: Optional[str]) -> str:
        """Resolve a skill id to a human-readable display name."""
        if not skill_id:
            return "default"
        for skill in self._load_skills():
            if str(getattr(skill, "name", "")).strip() == skill_id:
                display_name = str(getattr(skill, "display_name", "")).strip()
                return display_name or skill_id
        return skill_id

    @staticmethod
    def _build_execution_context(stock_code: str, skill_id: str) -> Dict[str, Any]:
        selected = [skill_id] if skill_id else []
        return {
            "stock_code": stock_code,
            "skills": selected,
            "strategies": selected,
        }

    @staticmethod
    def _build_user_message(stock_code: str, skill_id: str, skill_text: str) -> str:
        user_msg = f"请分析股票 {stock_code}"
        if skill_id:
            user_msg = f"请使用 {skill_id} 技能分析股票 {stock_code}"
        if skill_text:
            user_msg = f"请分析股票 {stock_code}，{skill_text}"
        return user_msg

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """Execute the ask command via Agent pipeline. Supports multi-stock."""
        config = get_config()

        if not config.agent_mode:
            return BotResponse.text_response(
                "⚠️ Agent 模式未开启，无法使用问股功能。\n请在配置中设置 `AGENT_MODE=true`。"
            )

        raw_code_str, remaining_args = self._merge_code_args(args)
        codes = self._parse_stock_codes(raw_code_str)

        # 检测 detail 模式：在剩余参数中查找 detail/详细/d，并从 skill_text 中移除
        detail_mode = False
        filtered_args = []
        for token in remaining_args:
            if token.lower() in ("detail", "详细", "d"):
                detail_mode = True
            else:
                filtered_args.append(token)
        remaining_args = filtered_args

        skill_id = self._parse_skill(["placeholder"] + remaining_args) if remaining_args else self._get_default_skill_id()
        skill_text = " ".join(remaining_args).strip()

        logger.info(
            "[AskCommand] Stocks: %s, Skill: %s, Extra: %s, Detail: %s",
            codes, skill_id, skill_text, detail_mode,
        )

        if len(codes) == 1:
            return self._analyze_single(config, message, codes[0], skill_id, skill_text, detail_mode)

        return self._analyze_multi(config, message, codes, skill_id, skill_text)

    def _analyze_single(
        self,
        config,
        message: BotMessage,
        code: str,
        skill_id: str,
        skill_text: str,
        detail_mode: bool = False,
    ) -> BotResponse:
        """Analyze a single stock.

        Args:
            detail_mode: 若为 True，输出完整原始分析（调试友好）；
                否则输出手机友好的结构化摘要，绝不通过截断详细报告生成。
        """
        try:
            from src.agent.factory import build_agent_executor

            executor = build_agent_executor(config, skills=[skill_id] if skill_id else None)
            user_msg = self._build_user_message(code, skill_id, skill_text)
            session_id = f"{message.platform}_{message.user_id}:ask_{code}_{uuid.uuid4()}"
            result = executor.chat(
                message=user_msg,
                session_id=session_id,
                context=self._build_execution_context(code, skill_id),
            )

            if result.success:
                skill_name = self._resolve_skill_name(skill_id)
                if detail_mode:
                    # 完整原始输出
                    header = f"📊 {code} | 技能: {skill_name}\n{'─' * 30}\n"
                    return BotResponse.text_response(header + result.content)

                # 手机友好结构化摘要：仅从 dashboard 字段重组，绝不截断详细报告
                dashboard = result.dashboard if isinstance(result.dashboard, dict) else None
                if dashboard:
                    summary = self._format_mobile_summary(code, skill_name, dashboard)
                    return BotResponse.markdown_response(summary)

                # 无可用 dashboard 时，引导用户使用 detail 模式，绝不能截断原始内容
                return BotResponse.text_response(
                    f"📊 {code} | 技能: {skill_name}\n\n"
                    f"⚠️ 无法生成简版报告，该分析结果不包含结构化数据。\n\n"
                    f"查看完整分析：/{self.name} {code} detail"
                )

            return BotResponse.text_response(f"⚠️ 分析失败: {result.error}")

        except Exception as exc:
            logger.error("Ask command failed: %s", exc)
            logger.exception("Ask error details:")
            return BotResponse.text_response(f"⚠️ 问股执行出错: {str(exc)}")

    @staticmethod
    def _format_mobile_summary(code: str, skill_name: str, dashboard: dict) -> str:
        """从 dashboard 生成手机友好的结构化摘要。

        固定结构（绝不通过截断详细报告生成）：
            📊 股票名称（代码）
            🎯 核心结论（买入/观望/减仓/卖出 + 综合评分 + 一句话）
            📈 关键依据（趋势/量价/估值/市场环境，最多 4 条）
            ⚠️ 主要风险（最多 3 条）
            🎯 操作点位（理想买入区/支撑位/止损位/压力位）
            🔄 触发条件（什么条件下转为买入，什么条件下失效）
            查看完整分析：/ask <代码> detail

        所有内部字段名（bull_trend、sentiment_score、ma_golden_cross 等）
        绝不直接暴露，必须映射为自然中文。
        """

        # ── 内部字段名 → 人类可读中文映射表 ──
        # 用于 dashboard 字段值中可能出现的 snake_case / internal ID 清理
        _ID_MAP = {
            "bull_trend": "多头趋势",
            "sentiment_score": "情绪评分",
            "ma_golden_cross": "均线金叉",
            "volume_breakout": "放量突破",
            "support_level": "支撑位",
            "resistance_level": "压力位",
            "stop_loss": "止损位",
            "take_profit": "目标位",
            "ideal_buy": "理想买入区",
            "secondary_buy": "次优买入区",
            "trend_prediction": "趋势预测",
            "operation_advice": "操作建议",
            "risk_warning": "风险提示",
            "analysis_summary": "分析摘要",
            "price_position": "价格位置",
            "volume_analysis": "量价分析",
            "chip_structure": "筹码结构",
            "turnover_rate": "换手率",
            "profit_ratio": "盈利比例",
            "avg_cost": "平均成本",
            "concentration": "筹码集中度",
            "phase_decision": "阶段决策",
            "watch_conditions": "观察条件",
            "immediate_action": "当前动作",
            "position_advice": "仓位建议",
            "sniper_points": "狙击点位",
            "signal_attribution": "信号归因",
            "intelligence": "情报分析",
            "core_conclusion": "核心结论",
            "battle_plan": "作战计划",
            "data_perspective": "数据视角",
            "trend_status": "趋势状态",
            "ma_alignment": "均线排列",
            "is_bullish": "看涨",
            "action_checklist": "执行清单",
            "suggested_position": "建议仓位",
            "risk_control": "风险控制",
            "entry_plan": "入场计划",
            "confidence_level": "置信度",
            "decision_type": "决策类型",
            "stock_name": "股票名称",
            "one_sentence": "一句话总结",
            "time_sensitivity": "时效性",
            "latest_news": "最新消息",
            "risk_alerts": "风险预警",
            "positive_catalysts": "正面催化剂",
            "earnings_outlook": "业绩展望",
            "sentiment_summary": "情绪摘要",
            "technical_indicators": "技术指标",
            "news_sentiment": "新闻情绪",
            "fundamentals": "基本面",
            "market_conditions": "市场环境",
            "strongest_bullish_signal": "最强看多信号",
            "strongest_bearish_signal": "最强看空信号",
        }

        def _safe(val: str) -> str:
            """去除空值标记，并清理内部 ID 字符串。"""
            if not val or str(val).strip() in ("", "-", "—", "N/A", "None", "无", "none"):
                return ""
            text = str(val).strip()
            # 将文本中出现的内部 ID 替换为中文
            for eng, cn in _ID_MAP.items():
                # 只替换完整单词（前后非字母数字）
                text = re.sub(r'\b' + re.escape(eng) + r'\b', cn, text)
            return text

        def _get_str(d: dict, *keys: str, default: str = "") -> str:
            """安全地从嵌套 dict 中提取字符串值。"""
            for key in keys:
                if not isinstance(d, dict):
                    return default
                d = d.get(key, {})
            return _safe(str(d)) if isinstance(d, str) else default

        stock_name = _safe(str(dashboard.get("stock_name") or code))

        # ── 决策映射 ──
        raw_decision = str(dashboard.get("decision_type", "")).strip().lower()
        decision_map = {"buy": "买入", "hold": "观望", "sell": "卖出", "reduce": "减仓"}
        decision_text = decision_map.get(raw_decision, raw_decision)

        # ── 综合评分（0-10）──
        sentiment = dashboard.get("sentiment_score")
        comprehensive_score = ""
        if isinstance(sentiment, (int, float)) and 0 <= sentiment <= 100:
            score_val = round(sentiment / 10.0, 1)
            comprehensive_score = f"综合评分：{score_val}/10"
        elif isinstance(sentiment, (int, float)):
            score_val = max(0, min(10, round(sentiment / 10.0)))
            comprehensive_score = f"综合评分：{score_val}/10"

        # ── 一句话原因 ──
        nested = dashboard.get("dashboard") or {}
        core = nested.get("core_conclusion") or {}
        one_sentence = _safe(str(core.get("one_sentence") or ""))

        # ── 信号归因（关键依据）──
        attribution = nested.get("signal_attribution") or {}
        evidence_items = []

        # 趋势
        dp = nested.get("data_perspective") or {}
        ts = dp.get("trend_status") or {}
        trend_text = _safe(str(ts.get("ma_alignment") or ""))
        if ts.get("is_bullish") is not None:
            trend_label = "看涨" if ts.get("is_bullish") else "看跌"
            if trend_text:
                evidence_items.append(f"趋势：{trend_text}（{trend_label}）")
            else:
                evidence_items.append(f"趋势：{trend_label}")

        # 量价
        volume = dp.get("volume_analysis") or {}
        vol_status = _safe(str(volume.get("volume_status") or ""))
        vol_ratio = volume.get("volume_ratio")
        if vol_status:
            evidence_items.append(f"量价：{vol_status}")
        elif isinstance(vol_ratio, (int, float)):
            evidence_items.append(f"量价：量比 {vol_ratio:.2f}")

        # 估值/基本面
        fundamentals_val = attribution.get("fundamentals")
        if isinstance(fundamentals_val, (int, float)) and fundamentals_val > 0:
            evidence_items.append(f"估值/基本面：贡献 {fundamentals_val:.0f}%")

        # 市场环境
        market_val = attribution.get("market_conditions")
        if isinstance(market_val, (int, float)) and market_val > 0:
            evidence_items.append(f"市场环境：贡献 {market_val:.0f}%")

        # 若信号归因不足 4 条，补充技术指标和新闻情绪
        tech_val = attribution.get("technical_indicators")
        if isinstance(tech_val, (int, float)) and tech_val > 0 and len(evidence_items) < 4:
            evidence_items.append(f"技术指标：贡献 {tech_val:.0f}%")
        news_val = attribution.get("news_sentiment")
        if isinstance(news_val, (int, float)) and news_val > 0 and len(evidence_items) < 4:
            evidence_items.append(f"新闻情绪：贡献 {news_val:.0f}%")

        # 最强信号补位
        if len(evidence_items) < 2:
            strong_bull = _safe(str(attribution.get("strongest_bullish_signal") or ""))
            strong_bear = _safe(str(attribution.get("strongest_bearish_signal") or ""))
            if strong_bull and strong_bull not in ("", "none", "无"):
                evidence_items.append(f"最强看多：{strong_bull}")
            if strong_bear and strong_bear not in ("", "none", "无") and len(evidence_items) < 4:
                evidence_items.append(f"最强看空：{strong_bear}")

        # 最多保留 4 条
        evidence_items = evidence_items[:4]

        # ── 主要风险 ──
        intelligence = nested.get("intelligence") or {}
        risk_alerts = intelligence.get("risk_alerts") or []
        raw_risk_warning = _safe(str(dashboard.get("risk_warning") or ""))

        risk_items = []
        for alert in risk_alerts:
            if len(risk_items) >= 3:
                break
            if isinstance(alert, str):
                txt = _safe(alert)
                if txt:
                    risk_items.append(txt)
            elif isinstance(alert, dict):
                desc = alert.get("description") or alert.get("alert") or ""
                txt = _safe(str(desc))
                if txt:
                    risk_items.append(txt)
        if raw_risk_warning and len(risk_items) < 3:
            risk_items.append(raw_risk_warning)

        # ── 操作点位 ──
        battle_plan = nested.get("battle_plan") or {}
        sniper = battle_plan.get("sniper_points") or {}

        def _sniper_val(key: str) -> str:
            v = AskCommand._format_sniper_value(sniper.get(key))
            return _safe(v) if v else ""

        ideal_buy = _sniper_val("ideal_buy")
        secondary_buy = _sniper_val("secondary_buy")
        stop_loss = _sniper_val("stop_loss")
        take_profit = _sniper_val("take_profit")

        # 支撑位：优先从 price_position 获取，其次用 secondary_buy
        support = _safe(str(dp.get("price_position", {}).get("support_level") or ""))
        if not support:
            support = secondary_buy

        # 压力位/目标位：优先 take_profit，其次 price_position.resistance_level
        resistance = _safe(str(dp.get("price_position", {}).get("resistance_level") or ""))
        if not resistance:
            resistance = take_profit

        # 是否有可靠点位
        has_reliable_points = any(v for v in [ideal_buy, support, stop_loss, resistance])

        # ── 触发条件 ──
        phase = nested.get("phase_decision") or {}
        watch_conditions = phase.get("watch_conditions") or []
        immediate_action = _safe(str(phase.get("immediate_action") or ""))
        next_check = _safe(str(phase.get("next_check_time") or ""))
        position_advice = core.get("position_advice") or {}

        trigger_lines = []
        if immediate_action:
            trigger_lines.append(f"• 当前动作：{immediate_action}")
        for wc in watch_conditions[:3]:
            wc_str = _safe(str(wc))
            if wc_str:
                trigger_lines.append(f"• {wc_str}")
        if next_check:
            trigger_lines.append(f"• 下次检查：{next_check}")
        # 空仓/持仓建议作为触发/失效条件的补充
        np_text = _safe(str(position_advice.get("no_position") or ""))
        hp_text = _safe(str(position_advice.get("has_position") or ""))
        if np_text and np_text not in ("", "-", "无"):
            trigger_lines.append(f"• 空仓者：{np_text}")
        if hp_text and hp_text not in ("", "-", "无"):
            trigger_lines.append(f"• 持仓者：{hp_text}")

        # ── 组装输出 ──
        lines = [
            f"📊 **{stock_name}（{code}）**",
            "",
        ]

        # 🎯 核心结论
        lines.append("🎯 **核心结论**")
        lines.append(decision_text)
        if comprehensive_score:
            lines.append(comprehensive_score)
        if one_sentence:
            lines.append(one_sentence)
        lines.append("")

        # 📈 关键依据
        if evidence_items:
            lines.append("📈 **关键依据**")
            lines.extend(evidence_items)
            lines.append("")

        # ⚠️ 主要风险
        if risk_items:
            lines.append("⚠️ **主要风险**")
            for item in risk_items[:3]:
                lines.append(f"• {item}")
            lines.append("")

        # 🎯 操作点位
        lines.append("🎯 **操作点位**")
        if has_reliable_points:
            if ideal_buy:
                lines.append(f"• 理想买入区：{ideal_buy}")
            if support:
                lines.append(f"• 支撑位：{support}")
            if stop_loss:
                lines.append(f"• 止损位：{stop_loss}")
            if resistance:
                lines.append(f"• 压力位/目标位：{resistance}")
        else:
            lines.append("暂无可靠点位")
        lines.append("")

        # 🔄 触发条件
        lines.append("🔄 **触发条件**")
        if trigger_lines:
            lines.extend(trigger_lines)
        else:
            # 从 phase_decision 中提取 basic 触发和失效语义
            trigger = _safe(str(phase.get("action_window") or ""))
            if trigger:
                lines.append(f"• {trigger}")
            else:
                lines.append("暂无明确触发条件，请结合市场走势判断")
        lines.append("")

        # 尾部引导
        lines.append(f"查看完整分析：/ask {code} detail")

        return "\n".join(lines).strip()

    def _analyze_multi(
        self,
        config,
        message: BotMessage,
        codes: List[str],
        skill_id: str,
        skill_text: str,
    ) -> BotResponse:
        """Analyze multiple stocks in parallel and produce a comparison summary."""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed

        skill_name = self._resolve_skill_name(skill_id)
        results: Dict[str, Dict[str, Any]] = {}
        errors: Dict[str, str] = {}
        started_at = time.monotonic()
        overall_timeout_s = self._MULTI_ANALYZE_TIMEOUT_S

        platform = message.platform
        user_id = message.user_id

        def _run_one(stock_code: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
            try:
                from src.agent.conversation import conversation_manager
                from src.agent.factory import build_agent_executor

                executor = build_agent_executor(config, skills=[skill_id] if skill_id else None)
                user_msg = self._build_user_message(stock_code, skill_id, skill_text)
                session_id = f"{platform}_{user_id}:ask_{stock_code}_{uuid.uuid4()}"
                conversation_manager.add_message(session_id, "user", user_msg)

                result = executor.run(
                    task=user_msg,
                    context=self._build_execution_context(stock_code, skill_id),
                )
                if result.success or self._should_accept_fallback_content(result):
                    dashboard = result.dashboard if isinstance(result.dashboard, dict) else None
                    formatted_analysis = self._format_stock_result(stock_code, dashboard, result.content)
                    conversation_manager.add_message(session_id, "assistant", formatted_analysis)
                    return (
                        stock_code,
                        {
                            "content": result.content,
                            "dashboard": dashboard,
                            "signal": self._extract_signal(dashboard),
                            "confidence": self._extract_confidence(dashboard),
                            "summary": self._extract_summary(stock_code, dashboard, result.content),
                            "markdown": formatted_analysis,
                            "stock_name": self._extract_stock_name(stock_code, dashboard),
                            "risk_flags": self._extract_risk_flags(dashboard),
                        },
                        None,
                    )

                error_note = f"[分析失败] {result.error or '未知错误'}"
                conversation_manager.add_message(session_id, "assistant", error_note)
                return (stock_code, None, result.error or "未知错误")
            except Exception as exc:
                return (stock_code, None, str(exc))

        # Warm up DB connections before parallel history writes.
        get_db()
        pool = ThreadPoolExecutor(max_workers=min(len(codes), 5))
        future_map = {pool.submit(_run_one, code): code for code in codes}
        try:
            for future in as_completed(future_map, timeout=overall_timeout_s):
                try:
                    code, content, error = future.result(timeout=5)
                    if content is not None:
                        results[code] = content
                    else:
                        errors[code] = error or "未知错误"
                except Exception as exc:
                    code = future_map[future]
                    errors[code] = f"执行异常: {exc}"
        except FutureTimeoutError:
            logger.warning("[AskCommand] Multi-stock analysis hit overall timeout (%.1fs)", overall_timeout_s)
            for future, code in future_map.items():
                if code in results or code in errors:
                    continue
                if future.done():
                    try:
                        code_done, content, error = future.result(timeout=0)
                        if content is not None:
                            results[code_done] = content
                        else:
                            errors[code] = error or "未知错误"
                    except Exception as exc:
                        errors[code] = f"执行异常: {exc}"
                else:
                    errors[code] = "分析超时（未在 150 秒内完成）"
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        for code in codes:
            if code not in results and code not in errors:
                errors[code] = "分析超时"

        parts = [f"📊 **多股对比分析** | 技能: {skill_name}", f"{'─' * 30}", ""]

        remaining_timeout_s = max(0.0, overall_timeout_s - (time.monotonic() - started_at))
        portfolio_section = self._build_portfolio_section(
            config,
            codes,
            results,
            timeout_s=remaining_timeout_s,
        )
        if portfolio_section:
            parts.append(portfolio_section)
            parts.append("")

        if len(results) >= 2:
            parts.append("| 股票 | 信号 | 置信度 | 摘要 |")
            parts.append("|------|------|--------|------|")
            for code in codes:
                if code in results:
                    item = results[code]
                    signal = item.get("signal") or "unknown"
                    confidence = item.get("confidence")
                    confidence_text = f"{confidence:.0%}" if isinstance(confidence, (int, float)) else "-"
                    summary_line = str(item.get("summary") or "分析完成").replace("|", "/")[:80]
                    parts.append(f"| {code} | {signal} | {confidence_text} | {summary_line} |")
                elif code in errors:
                    parts.append(f"| {code} | error | - | ⚠️ {errors[code][:40]} |")
            parts.append("")

        for code in codes:
            if code in results:
                parts.append(f"### {code}")
                parts.append(results[code]["markdown"])
                parts.append("")
            elif code in errors:
                parts.append(f"### {code}")
                parts.append(f"⚠️ 分析失败: {errors[code]}")
                parts.append("")

        return BotResponse.markdown_response("\n".join(parts))

    @staticmethod
    def _should_accept_fallback_content(result: Any) -> bool:
        """Keep usable free-form answers when dashboard JSON parsing fails."""
        if getattr(result, "success", False):
            return True

        content = getattr(result, "content", "")
        error = str(getattr(result, "error", "") or "")
        if not isinstance(content, str) or not content.strip():
            return False

        return error == "Failed to parse dashboard JSON from agent response"

    @staticmethod
    def _extract_stock_name(stock_code: str, dashboard: Optional[Dict[str, Any]]) -> str:
        if isinstance(dashboard, dict):
            stock_name = dashboard.get("stock_name")
            if isinstance(stock_name, str) and stock_name.strip():
                return stock_name.strip()
        return stock_code

    @staticmethod
    def _extract_signal(dashboard: Optional[Dict[str, Any]]) -> str:
        if isinstance(dashboard, dict):
            signal = dashboard.get("decision_type")
            if isinstance(signal, str) and signal.strip():
                return signal.strip()
        return "unknown"

    @staticmethod
    def _extract_confidence(dashboard: Optional[Dict[str, Any]]) -> Optional[float]:
        if not isinstance(dashboard, dict):
            return None

        score = dashboard.get("sentiment_score")
        try:
            return max(0.0, min(1.0, float(score) / 100.0))
        except (TypeError, ValueError):
            pass

        level = str(dashboard.get("confidence_level") or "").strip()
        return {"高": 0.85, "中": 0.65, "低": 0.45}.get(level)

    @staticmethod
    def _extract_summary(stock_code: str, dashboard: Optional[Dict[str, Any]], raw_content: str) -> str:
        if isinstance(dashboard, dict):
            for key in ("analysis_summary", "risk_warning", "trend_prediction"):
                value = dashboard.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            dashboard_block = dashboard.get("dashboard")
            if not isinstance(dashboard_block, dict):
                dashboard_block = {}
            core_conclusion = dashboard_block.get("core_conclusion")
            if not isinstance(core_conclusion, dict):
                core_conclusion = {}
            core = core_conclusion.get("one_sentence")
            if isinstance(core, str) and core.strip():
                return core.strip()

        for line in raw_content.splitlines():
            stripped = line.strip()
            if stripped and len(stripped) > 4 and not stripped.startswith(("{", "}", "\"")):
                return stripped[:120]
        return f"{stock_code} 分析完成"

    @staticmethod
    def _extract_risk_flags(dashboard: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
        if not isinstance(dashboard, dict):
            return []

        flags: List[Dict[str, str]] = []
        dashboard_block = dashboard.get("dashboard")
        if not isinstance(dashboard_block, dict):
            dashboard_block = {}
        intelligence = dashboard_block.get("intelligence")
        if not isinstance(intelligence, dict):
            intelligence = {}
        for alert in intelligence.get("risk_alerts", [])[:5]:
            if isinstance(alert, str) and alert.strip():
                flags.append({"category": "portfolio_input", "description": alert.strip(), "severity": "medium"})

        risk_warning = dashboard.get("risk_warning")
        if isinstance(risk_warning, str) and risk_warning.strip():
            flags.append({"category": "portfolio_input", "description": risk_warning.strip(), "severity": "medium"})
        return flags

    @staticmethod
    def _format_sniper_value(value: Any) -> Optional[str]:
        if value is None:
            return None

        text = str(value).strip()
        if not text or text in {"-", "—", "N/A", "None"}:
            return None

        prefixes = (
            "理想买入点：",
            "次优买入点：",
            "止损位：",
            "目标位：",
            "理想买入点:",
            "次优买入点:",
            "止损位:",
            "目标位:",
        )
        for prefix in prefixes:
            if text.startswith(prefix):
                stripped = text[len(prefix):].strip()
                return stripped or None

        return text

    @staticmethod
    def _format_stock_result(stock_code: str, dashboard: Optional[Dict[str, Any]], raw_content: str) -> str:
        if not isinstance(dashboard, dict):
            content = raw_content
            if len(content) > 800:
                content = content[:800] + "\n... (已截断，完整分析请单独查询)"
            return content

        lines = []
        stock_name = dashboard.get("stock_name")
        if isinstance(stock_name, str) and stock_name.strip() and stock_name.strip() != stock_code:
            lines.append(f"**名称**: {stock_name.strip()}")

        decision = dashboard.get("decision_type")
        confidence = AskCommand._extract_confidence(dashboard)
        trend = dashboard.get("trend_prediction")
        if isinstance(decision, str):
            lines.append(
                f"**结论**: {decision}"
                + (f" | **置信度**: {confidence:.0%}" if isinstance(confidence, (int, float)) else "")
                + (f" | **趋势**: {trend}" if isinstance(trend, str) and trend.strip() else "")
            )

        summary = AskCommand._extract_summary(stock_code, dashboard, raw_content)
        if summary:
            lines.append(f"**摘要**: {summary}")

        operation = dashboard.get("operation_advice")
        if isinstance(operation, str) and operation.strip():
            lines.append(f"**操作建议**: {operation.strip()}")

        risk_warning = dashboard.get("risk_warning")
        if isinstance(risk_warning, str) and risk_warning.strip():
            lines.append(f"**风险提示**: {risk_warning.strip()}")

        dashboard_block = dashboard.get("dashboard")
        if not isinstance(dashboard_block, dict):
            dashboard_block = {}
        battle_plan = dashboard_block.get("battle_plan")
        if not isinstance(battle_plan, dict):
            battle_plan = {}
        sniper = battle_plan.get("sniper_points")
        if isinstance(sniper, dict):
            price_parts = []
            for key in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit"):
                value = AskCommand._format_sniper_value(sniper.get(key))
                if value:
                    price_parts.append(f"{key}={value}")
            if price_parts:
                lines.append("**关键点位**: " + " | ".join(price_parts))

        return "\n\n".join(lines) if lines else raw_content[:800]

    def _build_portfolio_section(
        self,
        config,
        codes: List[str],
        results: Dict[str, Dict[str, Any]],
        timeout_s: Optional[float] = None,
    ) -> str:
        """Generate a portfolio-level overlay for multi-stock ask results."""
        if len(results) < 2:
            return ""

        if timeout_s is not None and timeout_s <= 0:
            logger.info("[AskCommand] Skip portfolio overlay because no timeout budget remains")
            return ""

        def _render_overlay() -> str:
            from src.agent.agents.portfolio_agent import PortfolioAgent
            from src.agent.factory import get_tool_registry
            from src.agent.llm_adapter import LLMToolAdapter
            from src.agent.protocols import AgentContext

            stock_opinions: Dict[str, Dict[str, Any]] = {}
            risk_flags: List[Dict[str, str]] = []
            stock_list: List[str] = []
            for code in codes:
                item = results.get(code)
                if not item:
                    continue
                stock_list.append(code)
                stock_opinions[code] = {
                    "signal": item.get("signal", "unknown"),
                    "confidence": item.get("confidence", 0.5),
                    "summary": item.get("summary", ""),
                    "stock_name": item.get("stock_name", code),
                }
                risk_flags.extend(item.get("risk_flags", []))

            ctx = AgentContext(query=f"Portfolio overlay for {', '.join(stock_list)}")
            ctx.data["stock_opinions"] = stock_opinions
            ctx.data["stock_list"] = stock_list
            ctx.risk_flags.extend(risk_flags[:10])

            agent = PortfolioAgent(
                tool_registry=get_tool_registry(),
                llm_adapter=LLMToolAdapter(config),
            )
            stage_result = agent.run(ctx)
            if not stage_result.success:
                return ""

            assessment = ctx.data.get("portfolio_assessment")
            if not isinstance(assessment, dict):
                return ""

            lines = ["## 组合视角", ""]
            summary = assessment.get("summary")
            if isinstance(summary, str) and summary.strip():
                lines.append(summary.strip())
                lines.append("")

            risk_score = assessment.get("portfolio_risk_score")
            if risk_score is not None:
                lines.append(f"- 组合风险分: {risk_score}")
            sector_warnings = assessment.get("sector_warnings") or []
            if sector_warnings:
                lines.append(f"- 行业集中: {'；'.join(str(item) for item in sector_warnings[:3])}")
            correlation_warnings = assessment.get("correlation_warnings") or []
            if correlation_warnings:
                lines.append(f"- 相关性风险: {'；'.join(str(item) for item in correlation_warnings[:3])}")
            rebalance = assessment.get("rebalance_suggestions") or []
            if rebalance:
                lines.append(f"- 调仓建议: {'；'.join(str(item) for item in rebalance[:3])}")
            positions = assessment.get("positions") or []
            if positions:
                position_parts = []
                for position in positions[:5]:
                    if not isinstance(position, dict):
                        continue
                    code = position.get("code")
                    weight = position.get("suggested_weight")
                    signal = position.get("signal")
                    if code and weight is not None:
                        try:
                            weight_text = f"{float(weight):.0%}"
                        except (TypeError, ValueError):
                            weight_text = str(weight)
                        suffix = f" ({signal})" if signal else ""
                        position_parts.append(f"{code}: {weight_text}{suffix}")
                if position_parts:
                    lines.append(f"- 建议仓位: {'；'.join(position_parts)}")

            return "\n".join(lines)

        if timeout_s is None:
            try:
                return _render_overlay()
            except Exception as exc:
                logger.warning("[AskCommand] Portfolio overlay failed: %s", exc)
                return ""

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_render_overlay)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeoutError:
            logger.warning("[AskCommand] Portfolio overlay timed out after %.2fs", timeout_s)
            return ""
        except Exception as exc:
            logger.warning("[AskCommand] Portfolio overlay failed: %s", exc)
            return ""
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
