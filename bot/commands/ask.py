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
                否则输出手机友好的结构化摘要。
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

                # 手机友好结构化摘要
                dashboard = result.dashboard if isinstance(result.dashboard, dict) else None
                if dashboard:
                    summary = self._format_mobile_summary(code, skill_name, dashboard)
                    return BotResponse.markdown_response(summary)
                # 无 dashboard 时的 fallback：截取前 600 字符
                content = (result.content or "").strip()
                if len(content) > 600:
                    content = content[:600] + "\n\n...(完整分析请使用 /ask <代码> detail)"
                return BotResponse.text_response(f"📊 {code} | 技能: {skill_name}\n{content}")

            return BotResponse.text_response(f"⚠️ 分析失败: {result.error}")

        except Exception as exc:
            logger.error("Ask command failed: %s", exc)
            logger.exception("Ask error details:")
            return BotResponse.text_response(f"⚠️ 问股执行出错: {str(exc)}")

    @staticmethod
    def _format_mobile_summary(code: str, skill_name: str, dashboard: dict) -> str:
        """从 dashboard 生成手机友好的结构化摘要。

        固定结构：核心结论 → 关键依据 → 主要风险 → 操作点位 → 触发/失效条件。
        内部字段名（bull_trend、sentiment_score、skill 等）不直接暴露。
        """
        stock_name = str(dashboard.get("stock_name") or code).strip()
        decision = str(dashboard.get("decision_type") or "").strip()
        sentiment = dashboard.get("sentiment_score")
        confidence = str(dashboard.get("confidence_level") or "").strip()

        # 置信度中文
        confidence_text = ""
        if isinstance(sentiment, (int, float)) and 0 <= sentiment <= 100:
            pct = sentiment / 100.0
            if pct >= 0.85:
                confidence_text = "高置信度"
            elif pct >= 0.65:
                confidence_text = "中等置信度"
            else:
                confidence_text = "低置信度"
        elif confidence:
            confidence_text = {"高": "高置信度", "中": "中等置信度", "低": "低置信度"}.get(confidence, confidence)

        # 决策类型 → 中文信号
        signal_map = {"buy": "🟢 看多/买入", "hold": "🟡 持有/观望", "sell": "🔴 看空/卖出"}
        signal_text = signal_map.get(decision.lower(), decision)

        lines = [
            f"📊 {stock_name}（{code}）",
            f"技能：{skill_name}",
            "",
        ]

        # ── 核心结论 ──
        nested = dashboard.get("dashboard") or {}
        core = nested.get("core_conclusion") or {}
        one_sentence = str(core.get("one_sentence") or "").strip()

        conclusion_parts = [f"**{signal_text}**"]
        if confidence_text:
            conclusion_parts.append(confidence_text)
        if one_sentence:
            conclusion_parts.append(f"—— {one_sentence}")

        lines.append("📌 **核心结论**")
        lines.append("　".join(conclusion_parts))
        lines.append("")

        # ── 关键依据 ──
        attribution = nested.get("signal_attribution") or {}
        evidence_items = []
        for label, key, unit in [
            ("技术指标", "technical_indicators", "贡献"),
            ("新闻舆情", "news_sentiment", "贡献"),
            ("基本面", "fundamentals", "贡献"),
            ("市场环境", "market_conditions", "贡献"),
        ]:
            val = attribution.get(key)
            if isinstance(val, (int, float)) and val > 0:
                evidence_items.append(f"• {label}：{val:.0f}% {unit}")

        # 从 data_perspective 提取简要趋势状态
        data_perspective = nested.get("data_perspective") or {}
        trend_status = data_perspective.get("trend_status") or {}
        if trend_status.get("is_bullish") is not None:
            trend_label = "看涨" if trend_status.get("is_bullish") else "看跌"
            ma_text = str(trend_status.get("ma_alignment") or "").strip()
            if ma_text:
                evidence_items.append(f"• 均线排列：{ma_text}（{trend_label}）")
            else:
                evidence_items.append(f"• 趋势判断：{trend_label}")

        # 最强信号
        strongest_bull = str(attribution.get("strongest_bullish_signal") or "").strip()
        strongest_bear = str(attribution.get("strongest_bearish_signal") or "").strip()
        if strongest_bull and strongest_bull not in ("", "none", "无"):
            evidence_items.append(f"• 最强看多：{strongest_bull}")
        if strongest_bear and strongest_bear not in ("", "none", "无"):
            evidence_items.append(f"• 最强看空：{strongest_bear}")

        if evidence_items:
            lines.append("📊 **关键依据**")
            lines.extend(evidence_items)
            lines.append("")

        # ── 主要风险 ──
        intelligence = nested.get("intelligence") or {}
        risk_alerts = intelligence.get("risk_alerts") or []
        risk_warning = str(dashboard.get("risk_warning") or "").strip()

        risk_items = []
        for alert in risk_alerts[:3]:
            if isinstance(alert, str) and alert.strip():
                risk_items.append(f"• {alert.strip()}")
            elif isinstance(alert, dict):
                desc = alert.get("description") or alert.get("alert") or ""
                if desc:
                    risk_items.append(f"• {str(desc).strip()}")
        if risk_warning and risk_warning not in ("", "无", "none"):
            risk_items.append(f"• {risk_warning}")

        if risk_items:
            lines.append("⚠️ **主要风险**")
            lines.extend(risk_items[:3])
            lines.append("")

        # ── 操作点位 ──
        battle_plan = nested.get("battle_plan") or {}
        sniper = battle_plan.get("sniper_points") or {}
        position_strategy = battle_plan.get("position_strategy") or {}

        # 核心结论中的 position_advice
        position_advice = core.get("position_advice") or {}

        point_lines = []
        for key, label in [("ideal_buy", "理想买入"), ("secondary_buy", "次优买入"),
                            ("stop_loss", "止损位"), ("take_profit", "目标位")]:
            val = AskCommand._format_sniper_value(sniper.get(key))
            if val:
                point_lines.append(f"• {label}：{val}")

        if point_lines:
            lines.append("🎯 **操作点位**")
            lines.extend(point_lines)
            lines.append("")

        # ── 触发/失效条件 ──
        phase = nested.get("phase_decision") or {}
        watch_conditions = phase.get("watch_conditions") or []
        immediate_action = str(phase.get("immediate_action") or "").strip()
        next_check = str(phase.get("next_check_time") or "").strip()

        condition_lines = []
        if immediate_action and immediate_action not in ("", "-", "无"):
            condition_lines.append(f"• 当前动作：{immediate_action}")
        for wc in watch_conditions[:3]:
            wc_str = str(wc).strip()
            if wc_str:
                condition_lines.append(f"• 观察条件：{wc_str}")
        if next_check:
            condition_lines.append(f"• 下次检查：{next_check}")

        # 空仓/持仓建议
        np_text = str(position_advice.get("no_position") or "").strip()
        hp_text = str(position_advice.get("has_position") or "").strip()
        if np_text and np_text not in ("", "-", "无"):
            condition_lines.append(f"• 空仓者：{np_text}")
        if hp_text and hp_text not in ("", "-", "无"):
            condition_lines.append(f"• 持仓者：{hp_text}")

        if condition_lines:
            lines.append("⏰ **触发/失效条件**")
            lines.extend(condition_lines)
            lines.append("")

        # ── 操作建议（兜底：如果上面都没有，展示 operation_advice）──
        if not point_lines and not condition_lines and not risk_items:
            op_advice = str(dashboard.get("operation_advice") or "").strip()
            if op_advice:
                lines.append("💡 **操作建议**")
                lines.append(f"• {op_advice}")
                lines.append("")

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
