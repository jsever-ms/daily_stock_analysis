# -*- coding: utf-8 -*-
"""Tests for Fast Pipeline final LLM summary call.

Verifies that:
- Mock normal LLM returns generate the 5-section summary, not fallback.
- LLM call failures (provider=error) fall back to raw data.
- LLM response parse failures (empty/short content) fall back to raw data.
- Exception during LLM call falls back to raw data.
"""

import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from bot.commands.ask import AskCommand
from bot.models import BotResponse


def _mock_quote(code: str) -> dict:
    return {
        "name": "首都在线",
        "price": 18.50,
        "change_pct": 2.35,
        "pe_ratio": 45.2,
        "pb_ratio": 3.8,
        "total_mv": 8_640_000_000,
        "circ_mv": 5_200_000_000,
        "volume_ratio": 1.25,
        "turnover_rate": 3.45,
    }


def _mock_history(code: str, days: int) -> dict:
    """Return 20 records with date + close so local trend computation works."""
    import datetime
    records = []
    for i in range(20):
        d = (datetime.date.today() - datetime.timedelta(days=20 - i)).isoformat()
        records.append({
            "date": d,
            "open": 18.0 + i * 0.05,
            "high": 18.2 + i * 0.05,
            "low": 17.8 + i * 0.05,
            "close": 18.0 + i * 0.05,
            "volume": 1_000_000 + i * 10_000,
        })
    return {"data": records}


def _make_mock_trend_result():
    """Create a mock TrendAnalysisResult with sample values."""
    from src.stock_analyzer import TrendAnalysisResult, TrendStatus, VolumeStatus, MACDStatus, RSIStatus, BuySignal

    result = TrendAnalysisResult(code="300846")
    result.trend_status = TrendStatus.BULL
    result.ma_alignment = "多头排列"
    result.trend_strength = 7.5
    result.ma5 = 18.2
    result.ma10 = 17.9
    result.ma20 = 17.5
    result.current_price = 18.50
    result.bias_ma5 = 2.1
    result.bias_ma10 = 3.5
    result.bias_ma20 = 5.2
    result.volume_status = VolumeStatus.HEAVY_VOLUME_UP
    result.volume_ratio_5d = 1.3
    result.volume_trend = "放量上攻"
    result.support_levels = [17.8, 17.2]
    result.resistance_levels = [19.0, 20.5]
    result.macd_status = MACDStatus.BULLISH
    result.macd_signal = "金叉"
    result.rsi_6 = 62.0
    result.rsi_12 = 55.0
    result.rsi_status = RSIStatus.NEUTRAL
    result.buy_signal = BuySignal.BUY
    result.signal_score = 7
    result.signal_reasons = ["均线多头", "MACD金叉", "放量突破"]
    result.risk_factors = ["大盘调整风险", "板块轮动"]
    return result


def _mock_news(code: str, _query: str) -> dict:
    return {
        "success": True,
        "results": [
            {"title": "首都在线签订重大合同"},
            {"title": "IDC行业景气度回升"},
        ],
    }


class FastPipelineSummaryTestCase(unittest.TestCase):
    """Verify Fast Pipeline LLM summary call behavior."""

    def setUp(self):
        self.command = AskCommand()
        self.config = SimpleNamespace(
            get=lambda key, default=None: {
                "LITELLM_MODEL": "gemini/gemini-3.1-pro-preview",
                "LLM_CHANNELS": "gemini,siliconflow",
            }.get(key, default),
        )

    def _mock_llm_response(self, content: str, provider: str = "gemini", model: str = "gemini/gemini-3.1-pro-preview"):
        """Create a mock LLMResponse-like object."""
        return SimpleNamespace(
            content=content,
            provider=provider,
            model=model,
        )

    def _five_section_summary(self) -> str:
        """Return a valid 5-section summary text."""
        return (
            "📊 **首都在线（300846）**\n\n"
            "🎯 **核心结论**\n"
            "买入\n"
            "综合评分：7/10\n"
            "均线多头排列，MACD金叉，放量突破，短期趋势向好\n\n"
            "📈 **关键依据**\n"
            "• 均线多头排列，趋势强度强势\n"
            "• MACD金叉，看涨信号明确\n"
            "• 放量上攻，量比1.25，资金介入明显\n"
            "• RSI(6) 62，中性偏强，仍有上行空间\n\n"
            "⚠️ **主要风险**\n"
            "• 大盘调整风险可能拖累个股\n"
            "• 板块轮动导致资金分流\n"
            "• PE 45.2偏高，估值压力\n\n"
            "🎯 **操作点位**\n"
            "• 理想买入区：17.80-18.20\n"
            "• 支撑位：17.80\n"
            "• 止损位：17.20\n"
            "• 压力位/目标位：19.00\n\n"
            "🔄 **触发条件**\n"
            "• 转强条件：放量突破19.00确认上升趋势\n"
            "• 失效条件：跌破17.20支撑位，趋势转弱"
        )

    def test_fast_pipeline_llm_ok_returns_five_section_summary(self):
        """Mock normal LLM response should produce 5-section summary, not fallback."""
        mock_trend_result = _make_mock_trend_result()
        with patch(
            "src.agent.tools.data_tools._handle_get_realtime_quote",
            side_effect=_mock_quote,
        ), patch(
            "src.agent.tools.data_tools._handle_get_daily_history",
            side_effect=_mock_history,
        ), patch(
            "src.agent.tools.search_tools._handle_search_stock_news",
            side_effect=_mock_news,
        ), patch(
            "src.stock_analyzer.StockTrendAnalyzer",
        ) as mock_analyzer_cls:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze.return_value = mock_trend_result
            mock_analyzer_cls.return_value = mock_analyzer

            with patch.object(
                self.command,
                "_build_fast_pipeline_prompt",
                return_value="mock prompt",
            ):
                mock_adapter = MagicMock()
                mock_adapter.call_text.return_value = self._mock_llm_response(
                    self._five_section_summary(),
                )
                with patch(
                    "src.agent.llm_adapter.LLMToolAdapter",
                    return_value=mock_adapter,
                ):
                    result = self.command._fast_pipeline_analyze(
                        self.config,
                        "300846",
                        "default",
                        "",
                    )

        self.assertIsInstance(result, BotResponse)
        self.assertIn("📊 **首都在线（300846）**", result.text)
        self.assertIn("🎯 **核心结论**", result.text)
        self.assertIn("📈 **关键依据**", result.text)
        self.assertIn("⚠️ **主要风险**", result.text)
        self.assertIn("🎯 **操作点位**", result.text)
        self.assertIn("🔄 **触发条件**", result.text)
        # 不包含 fallback 标记
        self.assertNotIn("AI分析摘要生成失败", result.text)
        # 包含时序信息
        self.assertIn("⏱ 数据获取", result.text)
        self.assertIn("AI总结", result.text)

    def test_fast_pipeline_llm_provider_error_falls_back(self):
        """LLM provider=error should trigger fallback to raw data."""
        mock_trend_result = _make_mock_trend_result()
        with patch(
            "src.agent.tools.data_tools._handle_get_realtime_quote",
            side_effect=_mock_quote,
        ), patch(
            "src.agent.tools.data_tools._handle_get_daily_history",
            side_effect=_mock_history,
        ), patch(
            "src.agent.tools.search_tools._handle_search_stock_news",
            side_effect=_mock_news,
        ), patch(
            "src.stock_analyzer.StockTrendAnalyzer",
        ) as mock_analyzer_cls:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze.return_value = mock_trend_result
            mock_analyzer_cls.return_value = mock_analyzer

            with patch.object(
                self.command,
                "_build_fast_pipeline_prompt",
                return_value="mock prompt",
            ):
                mock_adapter = MagicMock()
                mock_adapter.call_text.return_value = self._mock_llm_response(
                    "No API key configured",
                    provider="error",
                    model="",
                )
                with patch(
                    "src.agent.llm_adapter.LLMToolAdapter",
                    return_value=mock_adapter,
                ):
                    result = self.command._fast_pipeline_analyze(
                        self.config,
                        "300846",
                        "default",
                        "",
                    )

        self.assertIsInstance(result, BotResponse)
        # 包含 fallback 标记（而非五段式 LLM 摘要）
        self.assertIn("⚠️ AI 分析摘要生成失败，展示原始数据", result.text)
        # 包含原始数据
        self.assertIn("首都在线", result.text)
        self.assertIn("18.5", result.text)
        self.assertIn("⏱ 数据获取", result.text)
        self.assertIn("AI总结", result.text)

    def test_fast_pipeline_llm_empty_response_falls_back(self):
        """LLM returns empty content should trigger LLM_RESPONSE_PARSE_FAILED fallback."""
        mock_trend_result = _make_mock_trend_result()
        with patch(
            "src.agent.tools.data_tools._handle_get_realtime_quote",
            side_effect=_mock_quote,
        ), patch(
            "src.agent.tools.data_tools._handle_get_daily_history",
            side_effect=_mock_history,
        ), patch(
            "src.agent.tools.search_tools._handle_search_stock_news",
            side_effect=_mock_news,
        ), patch(
            "src.stock_analyzer.StockTrendAnalyzer",
        ) as mock_analyzer_cls:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze.return_value = mock_trend_result
            mock_analyzer_cls.return_value = mock_analyzer

            with patch.object(
                self.command,
                "_build_fast_pipeline_prompt",
                return_value="mock prompt",
            ):
                mock_adapter = MagicMock()
                mock_adapter.call_text.return_value = self._mock_llm_response(
                    "",
                    provider="gemini",
                )
                with patch(
                    "src.agent.llm_adapter.LLMToolAdapter",
                    return_value=mock_adapter,
                ):
                    result = self.command._fast_pipeline_analyze(
                        self.config,
                        "300846",
                        "default",
                        "",
                    )

        self.assertIsInstance(result, BotResponse)
        # 包含 fallback 标记（而非五段式 LLM 摘要）
        self.assertIn("⚠️ AI 分析摘要生成失败，展示原始数据", result.text)
        # 包含原始数据（fallback）
        self.assertIn("首都在线", result.text)

    def test_fast_pipeline_llm_short_response_falls_back(self):
        """LLM returns very short content (<50 chars) should trigger LLM_RESPONSE_PARSE_FAILED."""
        mock_trend_result = _make_mock_trend_result()
        with patch(
            "src.agent.tools.data_tools._handle_get_realtime_quote",
            side_effect=_mock_quote,
        ), patch(
            "src.agent.tools.data_tools._handle_get_daily_history",
            side_effect=_mock_history,
        ), patch(
            "src.agent.tools.search_tools._handle_search_stock_news",
            side_effect=_mock_news,
        ), patch(
            "src.stock_analyzer.StockTrendAnalyzer",
        ) as mock_analyzer_cls:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze.return_value = mock_trend_result
            mock_analyzer_cls.return_value = mock_analyzer

            with patch.object(
                self.command,
                "_build_fast_pipeline_prompt",
                return_value="mock prompt",
            ):
                mock_adapter = MagicMock()
                mock_adapter.call_text.return_value = self._mock_llm_response(
                    "短内容",  # len < 50
                    provider="gemini",
                )
                with patch(
                    "src.agent.llm_adapter.LLMToolAdapter",
                    return_value=mock_adapter,
                ):
                    result = self.command._fast_pipeline_analyze(
                        self.config,
                        "300846",
                        "default",
                        "",
                    )

        self.assertIsInstance(result, BotResponse)
        # 包含 fallback 标记（而非五段式 LLM 摘要）
        self.assertIn("⚠️ AI 分析摘要生成失败，展示原始数据", result.text)
        # 包含原始数据（fallback）
        self.assertIn("首都在线", result.text)

    def test_fast_pipeline_llm_exception_falls_back(self):
        """Exception during LLM call should trigger LLM_CALL_FAILED fallback."""
        mock_trend_result = _make_mock_trend_result()
        with patch(
            "src.agent.tools.data_tools._handle_get_realtime_quote",
            side_effect=_mock_quote,
        ), patch(
            "src.agent.tools.data_tools._handle_get_daily_history",
            side_effect=_mock_history,
        ), patch(
            "src.agent.tools.search_tools._handle_search_stock_news",
            side_effect=_mock_news,
        ), patch(
            "src.stock_analyzer.StockTrendAnalyzer",
        ) as mock_analyzer_cls:
            mock_analyzer = MagicMock()
            mock_analyzer.analyze.return_value = mock_trend_result
            mock_analyzer_cls.return_value = mock_analyzer

            with patch.object(
                self.command,
                "_build_fast_pipeline_prompt",
                return_value="mock prompt",
            ):
                mock_adapter = MagicMock()
                mock_adapter.call_text.side_effect = RuntimeError("Connection refused")
                with patch(
                    "src.agent.llm_adapter.LLMToolAdapter",
                    return_value=mock_adapter,
                ):
                    result = self.command._fast_pipeline_analyze(
                        self.config,
                        "300846",
                        "default",
                        "",
                    )

        self.assertIsInstance(result, BotResponse)
        # 包含 fallback 标记（而非五段式 LLM 摘要）
        self.assertIn("⚠️ AI 分析摘要生成失败，展示原始数据", result.text)
        # 包含原始数据（fallback）
        self.assertIn("首都在线", result.text)
        # 包含时序信息
        self.assertIn("⏱ 数据获取", result.text)
        self.assertIn("AI总结", result.text)


if __name__ == "__main__":
    unittest.main()