# -*- coding: utf-8 -*-
"""
===================================
Report Engine - Report renderer tests
===================================

Tests for Jinja2 report rendering and fallback behavior.
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from src.analyzer import AnalysisResult
from src.services.report_renderer import render


def _make_result(
    code: str = "600519",
    name: str = "贵州茅台",
    sentiment_score: int = 72,
    operation_advice: str = "持有",
    analysis_summary: str = "稳健",
    decision_type: str = "hold",
    dashboard: dict = None,
    report_language: str = "zh",
    model_used: str = None,
    trend_prediction: str = "看多",
) -> AnalysisResult:
    if dashboard is None:
        dashboard = {
            "core_conclusion": {"one_sentence": "持有观望"},
            "intelligence": {"risk_alerts": []},
            "battle_plan": {"sniper_points": {"stop_loss": "110"}},
        }
    return AnalysisResult(
        code=code,
        name=name,
        trend_prediction=trend_prediction,
        sentiment_score=sentiment_score,
        operation_advice=operation_advice,
        analysis_summary=analysis_summary,
        decision_type=decision_type,
        dashboard=dashboard,
        report_language=report_language,
        model_used=model_used,
    )


def _make_renderer_config(show_llm_model: bool = True) -> MagicMock:
    config = MagicMock()
    config.report_templates_dir = "templates"
    config.report_language = "zh"
    config.report_show_llm_model = show_llm_model
    return config


def _with_decision_signal_summary(result: AnalysisResult) -> AnalysisResult:
    result.decision_signal_summary = {
        "action": "sell",
        "action_label": "卖出",
        "horizon": "1d",
        "reason": "技术面走弱",
    }
    return result


class TestReportRenderer(unittest.TestCase):
    """Report renderer tests."""

    def test_render_markdown_summary_only(self) -> None:
        """Markdown platform renders with summary_only."""
        r = _make_result()
        out = render("markdown", [r], summary_only=True)
        self.assertIsNotNone(out)
        self.assertIn("决策仪表盘", out)
        self.assertIn("贵州茅台", out)
        self.assertIn("买入", out)
        self.assertIn("🟢买入:1", out)

    def test_render_markdown_preserves_guardrailed_neutral_action(self) -> None:
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "等待确认"},
                "decision_stability": {"applied": True, "reason": "等待回踩确认"},
            }
        )

        out = render("markdown", [r], summary_only=True)

        self.assertIsNotNone(out)
        self.assertIn("持有", out)
        self.assertIn("🟡观望:1", out)

    def test_render_markdown_uses_explicit_avoid_and_alert_text(self) -> None:
        avoid = _make_result(
            code="AVOID",
            name="Avoid Corp",
            sentiment_score=90,
            operation_advice="Buy",
            report_language="en",
        )
        avoid.action = "avoid"
        avoid.action_label = "Avoid"
        alert = _make_result(
            code="ALERT",
            name="Alert Corp",
            sentiment_score=85,
            operation_advice="Buy",
            report_language="en",
        )
        alert.action = "alert"
        alert.action_label = "Alert"

        out = render("markdown", [avoid, alert], summary_only=True)

        self.assertIsNotNone(out)
        self.assertIn("🟡 **Avoid Corp(AVOID)**: Avoid | Score 90", out)
        self.assertIn("🔴 **Alert Corp(ALERT)**: Alert | Score 85", out)
        self.assertIn("**Avoid Corp(AVOID)**: Avoid | Score 90", out)
        self.assertIn("**Alert Corp(ALERT)**: Alert | Score 85", out)
        self.assertNotIn("**Avoid Corp(AVOID)**: Buy", out)
        self.assertNotIn("**Alert Corp(ALERT)**: Buy", out)

    def test_render_markdown_full(self) -> None:
        """Markdown platform renders full report."""
        r = _make_result()
        out = render("markdown", [r], summary_only=False)
        self.assertIsNotNone(out)
        self.assertIn("决策仪表盘", out)
        self.assertIn("当前操作计划", out)
        self.assertNotIn("盘中决策护栏", out)

    def test_render_markdown_full_fixed_section_order(self) -> None:
        """详细报告固定九段结构且顺序正确。"""
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {
                    "sniper_points": {"stop_loss": "110"},
                    "action_checklist": ["必要条件：站稳MA20"],
                },
                "data_perspective": {
                    "trend_status": {"ma_alignment": "多头排列", "is_bullish": True, "trend_score": 70},
                    "price_position": {"current_price": 120},
                },
                "phase_decision": {
                    "action_window": "盘中跟踪",
                    "immediate_action": "等待确认",
                    "watch_conditions": ["放量突破"],
                    "next_check_time": "14:30",
                    "data_limitations": ["quote: stale"],
                },
            }
        )
        r.fundamental_analysis = "基本面稳健"

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        sections = [
            "决策仪表盘",
            "当前操作计划",
            "关键价位与风险收益",
            "技术与量价",
            "基本面估值",
            "新闻/公告/催化与风险",
            "条件检查",
            "下一观察点",
            "数据完整性",
        ]
        positions = [out.find(s) for s in sections]
        self.assertTrue(all(p >= 0 for p in positions), f"缺少段落: {[s for s, p in zip(sections, positions) if p < 0]}")
        self.assertEqual(positions, sorted(positions), "详细报告段落顺序不符合固定结构")

    def test_render_markdown_bearish_replaces_buy_points_with_observation_zone(self) -> None:
        """最终动作为卖出时，空仓者不得出现买入点/试仓点，只显示观察区与转强确认条件。"""
        r = _make_result(
            sentiment_score=20,
            operation_advice="减仓",
            decision_type="sell",
            trend_prediction="看空",
            dashboard={
                "core_conclusion": {"one_sentence": "反弹减仓"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {
                    "sniper_points": {
                        "ideal_buy": "17.0-17.2 轻仓试探",
                        "secondary_buy": "16.5 企稳后试仓",
                        "stop_loss": "16.0",
                        "take_profit": "18.5",
                    },
                },
                "data_perspective": {"price_position": {"current_price": 17.1}},
            },
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("当前结论偏空", out)
        self.assertIn("观察区", out)
        self.assertIn("转强确认条件", out)
        self.assertNotIn("理想买入点", out)
        self.assertNotIn("次优买入点", out)
        # 偏空时不得出现试仓/买入类表述
        self.assertNotIn("轻仓试探", out)
        self.assertNotIn("试仓", out)

    def test_render_markdown_non_bearish_keeps_buy_points(self) -> None:
        """非偏空结论仍正常显示入场位。"""
        r = _make_result(
            sentiment_score=75,
            decision_type="buy",
            trend_prediction="看多",
            dashboard={
                "core_conclusion": {"one_sentence": "回踩确认后买入"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {
                    "sniper_points": {
                        "ideal_buy": "110",
                        "secondary_buy": "108",
                        "stop_loss": "105",
                        "take_profit": "125",
                    },
                },
                "data_perspective": {"price_position": {"current_price": 112}},
            },
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("理想买入点", out)
        self.assertNotIn("当前结论偏空", out)

    def test_render_markdown_computes_risk_reward_programmatically(self) -> None:
        """盈亏比由程序计算：目标位 125、止损 105、现价 112 → 13/7 ≈ 1.9:1。"""
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {
                    "sniper_points": {
                        "ideal_buy": "110",
                        "stop_loss": "105",
                        "take_profit": "125",
                    },
                },
                "data_perspective": {"price_position": {"current_price": 112}},
            }
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("1.9:1", out)
        self.assertIn("价位说明", out)

    def test_render_markdown_risk_reward_unfavorable_warning(self) -> None:
        """盈亏比 < 1 时提示当前开仓性价比不足。"""
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {
                    "sniper_points": {
                        "stop_loss": "110",
                        "take_profit": "113",
                    },
                },
                "data_perspective": {"price_position": {"current_price": 112}},
            }
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("0.5:1", out)
        self.assertIn("当前不具备理想开仓性价比", out)

    def test_render_markdown_signal_attribution_qualitative_no_percent(self) -> None:
        """信号归因为定性表述（主导因素/主要看多/主要看空），不显示百分比。"""
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
                "signal_attribution": {
                    "dominant_factor": "技术面",
                    "strongest_bullish_signal": "MACD金叉",
                    "strongest_bearish_signal": "跌破MA20",
                },
            }
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("主导因素", out)
        self.assertIn("主要看多因素", out)
        self.assertIn("主要看空因素", out)
        self.assertNotIn("归因权重", out)

    def test_render_markdown_omits_decision_signal_excerpt(self) -> None:
        """Markdown reports omit the duplicated DecisionSignal excerpt."""
        r = _with_decision_signal_summary(_make_result())

        summary_out = render("markdown", [r], summary_only=True)
        self.assertIsNotNone(summary_out)
        self.assertNotIn("AI 决策信号", summary_out)

        full_out = render("markdown", [r], summary_only=False)
        self.assertIsNotNone(full_out)
        self.assertNotIn("AI 决策信号", full_out)
        self.assertNotIn("理由: 技术面走弱", full_out)

    def test_render_markdown_phase_decision_section(self) -> None:
        """Markdown renders phase_decision when present."""
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "等待确认"},
                "intelligence": {"risk_alerts": []},
                "phase_decision": {
                    "action_window": "盘中跟踪",
                    "immediate_action": "等待确认",
                    "watch_conditions": ["放量突破"],
                    "next_check_time": "14:30",
                    "confidence_reason": "数据质量可用",
                    "data_limitations": ["quote: stale"],
                },
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            }
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("下一观察点", out)
        self.assertIn("等待确认", out)
        self.assertIn("放量突破", out)
        self.assertIn("quote: stale", out)

    def test_render_markdown_skips_context_only_phase_decision_shape(self) -> None:
        """Markdown skips mechanically shaped phase_decision without actionable content."""
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "intelligence": {"risk_alerts": []},
                "phase_decision": {
                    "phase_context": {"phase": "intraday", "market": "cn"},
                    "action_window": None,
                    "immediate_action": None,
                    "watch_conditions": [],
                    "next_check_time": None,
                    "confidence_reason": None,
                    "data_limitations": [],
                },
                "battle_plan": {"sniper_points": {"stop_loss": "110"}},
            }
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertNotIn("下一观察点", out)

    def test_render_wechat(self) -> None:
        """Wechat platform renders."""
        r = _make_result()
        out = render("wechat", [r])
        self.assertIsNotNone(out)
        self.assertIn("贵州茅台", out)

    def test_render_wechat_omits_decision_signal_excerpt(self) -> None:
        """Wechat reports omit the duplicated DecisionSignal excerpt."""
        r = _with_decision_signal_summary(_make_result())

        summary_out = render("wechat", [r], summary_only=True)
        self.assertIsNotNone(summary_out)
        self.assertNotIn("AI 决策信号", summary_out)

        full_out = render("wechat", [r], summary_only=False)
        self.assertIsNotNone(full_out)
        self.assertNotIn("AI 决策信号", full_out)
        self.assertNotIn("理由: 技术面走弱", full_out)

    def test_render_brief(self) -> None:
        """Brief platform renders 3-5 sentence summary."""
        r = _make_result()
        out = render("brief", [r])
        self.assertIsNotNone(out)
        self.assertIn("决策简报", out)
        self.assertIn("贵州茅台", out)

    def test_render_brief_omits_decision_signal_excerpt(self) -> None:
        r = _with_decision_signal_summary(_make_result())

        out = render("brief", [r])

        self.assertIsNotNone(out)
        self.assertNotIn("AI 决策信号", out)

    def test_render_brief_respects_model_visibility_toggle(self) -> None:
        r = _make_result(model_used="gemini/gemini-2.5-flash")

        with patch("src.services.report_renderer.get_config", return_value=_make_renderer_config(True)):
            visible = render("brief", [r])
        with patch("src.services.report_renderer.get_config", return_value=_make_renderer_config(False)):
            hidden = render("brief", [r])

        self.assertIsNotNone(visible)
        self.assertIsNotNone(hidden)
        self.assertIn("分析模型: gemini/gemini-2.5-flash", visible)
        self.assertNotIn("分析模型", hidden)
        self.assertNotIn("gemini/gemini-2.5-flash", hidden)

    def test_render_templates_show_compact_market_status_only(self) -> None:
        r = _make_result()
        r.market_phase_summary = {
            "phase": "intraday",
            "market": "cn",
            "trigger_source": "api",
            "is_partial_bar": True,
        }
        r.analysis_context_pack_overview = {
            "data_quality": {
                "level": "limited",
                "limitations": ["quote: stale", "news: missing", "technical: fallback"],
            }
        }
        r.raw_response = "raw context pack should not appear"

        out = render("brief", [r])

        self.assertIsNotNone(out)
        self.assertIn("市场状态：A股 · 盘中", out)
        self.assertNotIn("阶段：intraday", out)
        self.assertNotIn("盘中数据提示", out)
        self.assertNotIn("数据质量: limited", out)
        self.assertNotIn("限制: quote: stale", out)
        self.assertNotIn("限制: news: missing", out)
        self.assertNotIn("technical: fallback", out)
        self.assertNotIn("raw context pack", out)

    def test_render_templates_skip_phase_pack_excerpt_when_summary_missing(self) -> None:
        r = _make_result()

        out = render("brief", [r])

        self.assertIsNotNone(out)
        self.assertNotIn("摘要来源", out)
        self.assertNotIn("evaluator snapshot", out)

    def test_render_market_status_preserves_input_order(self) -> None:
        cn = _make_result(
            code="600519",
            name="贵州茅台",
            sentiment_score=60,
        )
        cn.market_phase_summary = {"market": "cn", "phase": "postmarket"}
        us = _make_result(
            code="AAPL",
            name="Apple",
            sentiment_score=90,
        )
        us.market_phase_summary = {"market": "us", "phase": "premarket"}

        out = render("markdown", [cn, us], summary_only=True)

        self.assertIsNotNone(out)
        self.assertIn("市场状态：A股 · 盘后", out)
        self.assertNotIn("市场状态：美股 · 盘前", out)

    def test_render_markdown_footer_uses_consistent_separator(self) -> None:
        r = _make_result(model_used="gemini/gemini-2.5-flash")

        with patch("src.services.report_renderer.get_config", return_value=_make_renderer_config(True)):
            out = render("markdown", [r], summary_only=True)

        self.assertIsNotNone(out)
        self.assertIn("报告生成时间：", out)
        self.assertIn("分析模型：gemini/gemini-2.5-flash", out)
        self.assertNotIn("分析模型: gemini/gemini-2.5-flash", out)

    def test_render_markdown_in_english(self) -> None:
        """Markdown renderer switches headings and summary labels for English reports."""
        r = _make_result(
            name="Kweichow Moutai",
            operation_advice="Buy",
            analysis_summary="Momentum remains constructive.",
            report_language="en",
        )
        out = render("markdown", [r], summary_only=True)
        self.assertIsNotNone(out)
        self.assertIn("Decision Dashboard", out)
        self.assertIn("Summary", out)
        self.assertIn("Buy", out)

    def test_render_markdown_market_snapshot_uses_template_context(self) -> None:
        """Market snapshot macro should render localized labels with template context."""
        r = _make_result(
            code="AAPL",
            name="Apple",
            operation_advice="Buy",
            report_language="en",
        )
        r.market_snapshot = {
            "close": "180.10",
            "prev_close": "178.25",
            "open": "179.00",
            "high": "181.20",
            "low": "177.80",
            "pct_chg": "+1.04%",
            "change_amount": "1.85",
            "amplitude": "1.91%",
            "volume": "1200000",
            "amount": "215000000",
            "price": "180.35",
            "volume_ratio": "1.2",
            "turnover_rate": "0.8%",
            "source": "polygon",
        }

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("Market Snapshot", out)
        self.assertIn("Volume Ratio", out)

    def test_render_markdown_collapses_unavailable_chip_structure(self) -> None:
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "data_perspective": {
                    "chip_structure": {
                        "profit_ratio": "数据缺失，无法判断",
                        "avg_cost": "数据缺失，无法判断",
                        "concentration": "数据缺失，无法判断",
                        "chip_health": "数据缺失，无法判断",
                    }
                },
            }
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("**筹码**: 筹码分布未启用或数据源暂不可用，未纳入筹码判断。", out)
        self.assertEqual(out.count("数据缺失，无法判断"), 0)

    def test_render_markdown_renders_strategy_synthesis_with_localized_labels(self) -> None:
        r = _make_result(
            dashboard={
                "core_conclusion": {"one_sentence": "持有观望"},
                "strategy_synthesis": {
                    "final_signal": "buy",
                    "confidence": 0.8,
                    "conflict_count": 1,
                    "conflict_severity": "medium",
                    "consensus_level": "medium",
                    "summary_key": "strategy_synthesis.with_conflicts",
                    "summary_params": {
                        "opinion_count": 2,
                        "final_signal": "buy",
                        "consensus_level": "medium",
                        "conflict_severity": "medium",
                        "conflict_count": 1,
                    },
                    "supporting_skills": [{"skill_id": "bull_trend", "signal": "buy", "confidence": 0.8}],
                    "opposing_skills": [{"skill_id": "hot_theme", "signal": "sell", "confidence": 0.75}],
                    "conflicts": [
                        {
                            "conflict_type": "directional_opposition",
                            "severity": "medium",
                            "description_key": "strategy_conflict.directional_opposition",
                            "participants": ["bull_trend", "hot_theme"],
                        }
                    ],
                },
            }
        )

        out = render("markdown", [r], summary_only=False)

        self.assertIsNotNone(out)
        self.assertIn("多策略综合", out)
        self.assertIn("综合信号: 买入", out)
        self.assertIn("综合信号: 买入 | 共识度: 中", out)
        self.assertNotIn("bull_trend/买入", out)

    def test_render_templates_handle_legacy_strategy_synthesis_shapes(self) -> None:
        for platform in ("markdown", "wechat"):
            for malformed in ("bad-shape", ["bad-shape"], 42, True):
                result = _make_result(
                    dashboard={
                        "core_conclusion": {"one_sentence": "持有观望"},
                        "intelligence": {},
                        "battle_plan": {},
                        "strategy_synthesis": malformed,
                    }
                )

                out = render(platform, [result], summary_only=False)

                self.assertIsNotNone(out)
                self.assertNotIn("多策略综合", out)

            result = _make_result(
                dashboard={
                    "core_conclusion": {"one_sentence": "持有观望"},
                    "intelligence": {},
                    "battle_plan": {},
                    "strategy_synthesis": {
                        "final_signal": "hold",
                        "consensus_level": "insufficient",
                        "conflict_severity": "none",
                        "conflict_count": 0,
                        "supporting_skills": "bad-shape",
                        "opposing_skills": ["bad-shape"],
                        "conflicts": "bad-shape",
                        "summary_params": {"invalid_opinion_count": "3"},
                    },
                }
            )

            out = render(platform, [result], summary_only=False)

            self.assertIsNotNone(out)
            self.assertIn("多策略综合", out)
            self.assertIn("持有", out)

    def test_render_unknown_platform_returns_none(self) -> None:
        """Unknown platform returns None (caller fallback)."""
        r = _make_result()
        out = render("unknown_platform", [r])
        self.assertIsNone(out)

    def test_render_empty_results_returns_content(self) -> None:
        """Empty results still produces header."""
        out = render("markdown", [], summary_only=True)
        self.assertIsNotNone(out)
        self.assertIn("0", out)
