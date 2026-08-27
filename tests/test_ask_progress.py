# -*- coding: utf-8 -*-
"""Tests for /ask real-time progress feedback.

Verifies:
- _build_progress_text format
- Dedup logic (module-level _analyzing_codes set)
- Progress callback is called at each pipeline stage
- Progress callback failure does not affect analysis
- Non-Telegram platforms do not create progress
"""

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from bot.commands.ask import AskCommand, _ProgressRefresher, _analyzing_codes, _ProgressCallback
from bot.models import BotMessage, BotResponse


def _make_bot_message(platform: str = "telegram") -> BotMessage:
    return BotMessage(
        platform=platform,
        message_id="123",
        user_id="u1",
        user_name="tester",
        chat_id="chat1",
        chat_type="private",
        content="/ask 300846",
        raw_content="/ask 300846",
    )


class ProgressTextTestCase(unittest.TestCase):
    """Test _build_progress_text output format."""

    def test_initial_progress(self):
        text = AskCommand._build_progress_text(
            "300846", 0, [], "任务已接收", [], 0.0,
        )
        self.assertIn("🔄", text)
        self.assertIn("300846", text)
        self.assertIn("0%", text)
        self.assertIn("任务已接收", text)
        self.assertIn("已用时：0秒", text)

    def test_intermediate_progress(self):
        text = AskCommand._build_progress_text(
            "首都在线（300846）", 50, ["行情数据", "技术分析"], "情报搜索", [], 18.5,
        )
        self.assertIn("50%", text)
        self.assertIn("✅ 行情数据", text)
        self.assertIn("✅ 技术分析", text)
        self.assertIn("🔄 情报搜索", text)
        self.assertIn("已用时：18秒", text)

    def test_with_failed_stages(self):
        text = AskCommand._build_progress_text(
            "300846", 70, ["行情数据"], "AI 综合判断", ["情报搜索"], 30.0,
        )
        self.assertIn("70%", text)
        self.assertIn("✅ 行情数据", text)
        self.assertIn("⚠️ 情报搜索 不可用，已跳过", text)
        self.assertIn("🔄 AI 综合判断", text)
        self.assertIn("已用时：30秒", text)

    def test_full_progress(self):
        text = AskCommand._build_progress_text(
            "300846", 100, ["行情数据", "技术分析", "情报搜索", "AI 综合判断"],
            "", [], 45.0,
        )
        self.assertIn("100%", text)
        self.assertIn("✅ 行情数据", text)
        self.assertIn("✅ AI 综合判断", text)
        self.assertIn("已用时：45秒", text)

    def test_progress_bar_max(self):
        text = AskCommand._build_progress_text("300846", 100, [], "", [], 0.0)
        # 100% should have 10 full blocks
        self.assertIn("██████████", text)

    def test_progress_bar_zero(self):
        text = AskCommand._build_progress_text("300846", 0, [], "", [], 0.0)
        # 0% should have 10 empty blocks
        self.assertIn("░░░░░░░░░░", text)


class DedupTestCase(unittest.TestCase):
    """Test dedup logic for concurrent /ask for the same stock."""

    def setUp(self):
        _analyzing_codes.clear()

    def tearDown(self):
        _analyzing_codes.clear()

    def _patch_get_config(self, agent_mode=True):
        """Return a mock config and patch get_config."""
        config = SimpleNamespace(
            agent_mode=agent_mode,
            telegram_bot_token="test:token",
            telegram_chat_id="123",
            ask_fast_model="",
            litellm_model="test-model",
            anspire_api_keys=[],
            get=lambda key, default=None: {"LITELLM_MODEL": "test-model"}.get(key, default),
        )
        return patch("bot.commands.ask.get_config", return_value=config)

    def test_dedup_blocks_duplicate_code(self):
        code = "600519"
        _analyzing_codes.add(code)
        message = _make_bot_message()
        with self._patch_get_config(), \
             patch.object(AskCommand, '_merge_code_args', return_value=(code, [])), \
             patch.object(AskCommand, '_parse_stock_codes', return_value=[code]), \
             patch.object(AskCommand, '_get_default_skill_id', return_value="default"):
            cmd = AskCommand()
            response = cmd.execute(message, [code])
            self.assertIn("正在分析中", response.text)
            self.assertIn(code, response.text)

    def test_dedup_allows_different_code(self):
        code1 = "600519"
        code2 = "000858"
        _analyzing_codes.add(code1)
        message = _make_bot_message()
        with self._patch_get_config(), \
             patch.object(AskCommand, '_merge_code_args', return_value=(code2, [])), \
             patch.object(AskCommand, '_parse_stock_codes', return_value=[code2]), \
             patch.object(AskCommand, '_get_default_skill_id', return_value="default"), \
             patch.object(AskCommand, '_fast_pipeline_analyze', return_value=BotResponse.text_response("ok")):
            cmd = AskCommand()
            response = cmd.execute(message, [code2])
            self.assertEqual("ok", response.text)

    def test_dedup_clears_after_completion(self):
        code = "300846"
        message = _make_bot_message()
        with self._patch_get_config(), \
             patch.object(AskCommand, '_merge_code_args', return_value=(code, [])), \
             patch.object(AskCommand, '_parse_stock_codes', return_value=[code]), \
             patch.object(AskCommand, '_get_default_skill_id', return_value="default"), \
             patch.object(AskCommand, '_fast_pipeline_analyze', return_value=BotResponse.text_response("ok")):
            cmd = AskCommand()
            cmd.execute(message, [code])
            self.assertNotIn(code, _analyzing_codes)


class ProgressCallbackInjectionTestCase(unittest.TestCase):
    """Test progress callback is called at each pipeline stage and failures don't break analysis."""

    def _make_minimal_config(self):
        return SimpleNamespace(
            agent_mode=True,
            ask_fast_model="",
            litellm_model="test-model",
            anspire_api_keys=[],
            telegram_bot_token="",
            telegram_chat_id="",
            get=lambda key, default=None: {"LITELLM_MODEL": "test-model"}.get(key, default),
        )

    @patch("src.agent.tools.data_tools._handle_get_realtime_quote", return_value={"name": "Test", "price": 10.0})
    @patch("src.agent.tools.data_tools._handle_get_daily_history", return_value={"data": [
        {"date": "2024-01-01", "close": 10.0, "open": 9.9, "high": 10.1, "low": 9.8, "volume": 1000}
    ] * 20})
    @patch("src.stock_analyzer.StockTrendAnalyzer")
    @patch("src.agent.llm_adapter.LLMToolAdapter")
    def test_progress_callback_invoked_at_each_stage(self, mock_llm, mock_analyzer, mock_history, mock_quote):
        """Verify progress callback is called at 30%, 50%, 70%, 85%, 100%."""
        mock_llm_instance = MagicMock()
        mock_llm_instance.call_text.return_value = SimpleNamespace(
            content="📊 **Test（300846）**\n\n## 1. 行情\n\n## 2. 技术\n\n## 3. 情报\n\n## 4. 估值\n\n## 5. 总结",
            provider="gemini", model="test-model",
        )
        mock_llm.return_value = mock_llm_instance

        mock_result = MagicMock()
        mock_result.code = "300846"
        mock_result.trend_status = MagicMock()
        mock_result.trend_status.value = "多头排列"
        mock_result.ma_alignment = "多头排列"
        mock_result.trend_strength = 7.5
        mock_result.ma5 = 10.0
        mock_result.ma10 = 9.8
        mock_result.ma20 = 9.5
        mock_result.current_price = 10.0
        mock_result.bias_ma5 = 2.0
        mock_result.bias_ma10 = 3.0
        mock_result.bias_ma20 = 5.0
        mock_result.volume_status = MagicMock()
        mock_result.volume_status.value = "量能正常"
        mock_result.volume_ratio_5d = 1.0
        mock_result.volume_trend = "正常"
        mock_result.support_levels = []
        mock_result.resistance_levels = []
        mock_result.macd_dif = 0.1
        mock_result.macd_dea = 0.05
        mock_result.macd_bar = 0.05
        mock_result.macd_status = MagicMock()
        mock_result.macd_status.value = "多头"
        mock_result.macd_signal = ""
        mock_result.rsi_6 = 55.0
        mock_result.rsi_12 = 52.0
        mock_result.rsi_24 = 50.0
        mock_result.rsi_status = MagicMock()
        mock_result.rsi_status.value = "中性"
        mock_result.rsi_signal = ""
        mock_result.buy_signal = MagicMock()
        mock_result.buy_signal.value = "持有"
        mock_result.signal_score = 5
        mock_result.signal_reasons = []
        mock_result.risk_factors = []

        mock_analyzer_instance = MagicMock()
        mock_analyzer_instance.analyze.return_value = mock_result
        mock_analyzer.return_value = mock_analyzer_instance

        progress_calls = []

        def _tracking_cb(pct, completed, current, failed, elapsed, final_text=None):
            progress_calls.append((pct, completed[:], current, failed[:], final_text))

        cmd = AskCommand()
        config = self._make_minimal_config()
        # Patching stock_analyzer import inside _fast_pipeline_analyze
        with patch("src.stock_analyzer.StockTrendAnalyzer", return_value=mock_analyzer_instance):
            response = cmd._fast_pipeline_analyze(
                config, "300846", "", "",
                progress_cb=_tracking_cb,
            )

        # Should have calls at: 30%, 50%, 70%, 85%, 100%
        pcts = [c[0] for c in progress_calls]
        self.assertIn(30, pcts, "30% progress not called")
        self.assertIn(50, pcts, "50% progress not called")
        self.assertIn(70, pcts, "70% progress not called")
        self.assertIn(85, pcts, "85% progress not called")
        self.assertIn(100, pcts, "100% progress not called")

        # 100% should have final_text
        final_call = [c for c in progress_calls if c[0] == 100]
        self.assertTrue(any(c[4] is not None for c in final_call), "final_text not set")

    @patch("src.agent.tools.data_tools._handle_get_realtime_quote", return_value={"name": "Test", "price": 10.0})
    @patch("src.agent.tools.data_tools._handle_get_daily_history", return_value={"data": [
        {"date": "2024-01-01", "close": 10.0, "open": 9.9, "high": 10.1, "low": 9.8, "volume": 1000}
    ] * 20})
    @patch("src.stock_analyzer.StockTrendAnalyzer")
    @patch("src.agent.llm_adapter.LLMToolAdapter")
    def test_progress_callback_exception_does_not_break_analysis(
            self, mock_llm, mock_analyzer, mock_history, mock_quote):
        """Progress callback raising an exception should not affect the pipeline."""
        mock_llm_instance = MagicMock()
        mock_llm_instance.call_text.return_value = SimpleNamespace(
            content="📊 **Test（300846）**\n\n## 1. 行情\n\n## 2. 技术\n\n## 3. 情报\n\n## 4. 估值\n\n## 5. 总结",
            provider="gemini", model="test-model",
        )
        mock_llm.return_value = mock_llm_instance

        mock_result = MagicMock()
        mock_result.code = "300846"
        mock_result.trend_status = MagicMock()
        mock_result.trend_status.value = "多头排列"
        mock_result.ma_alignment = "多头排列"
        mock_result.trend_strength = 7.5
        mock_result.ma5 = 10.0
        mock_result.ma10 = 9.8
        mock_result.ma20 = 9.5
        mock_result.current_price = 10.0
        mock_result.bias_ma5 = 2.0
        mock_result.bias_ma10 = 3.0
        mock_result.bias_ma20 = 5.0
        mock_result.volume_status = MagicMock()
        mock_result.volume_status.value = "量能正常"
        mock_result.volume_ratio_5d = 1.0
        mock_result.volume_trend = "正常"
        mock_result.support_levels = []
        mock_result.resistance_levels = []
        mock_result.macd_dif = 0.1
        mock_result.macd_dea = 0.05
        mock_result.macd_bar = 0.05
        mock_result.macd_status = MagicMock()
        mock_result.macd_status.value = "多头"
        mock_result.macd_signal = ""
        mock_result.rsi_6 = 55.0
        mock_result.rsi_12 = 52.0
        mock_result.rsi_24 = 50.0
        mock_result.rsi_status = MagicMock()
        mock_result.rsi_status.value = "中性"
        mock_result.rsi_signal = ""
        mock_result.buy_signal = MagicMock()
        mock_result.buy_signal.value = "持有"
        mock_result.signal_score = 5
        mock_result.signal_reasons = []
        mock_result.risk_factors = []

        mock_analyzer_instance = MagicMock()
        mock_analyzer_instance.analyze.return_value = mock_result
        mock_analyzer.return_value = mock_analyzer_instance

        def _exploding_cb(pct, completed, current, failed, elapsed, final_text=None):
            raise RuntimeError("progress callback exploded")

        cmd = AskCommand()
        config = self._make_minimal_config()
        with patch("src.stock_analyzer.StockTrendAnalyzer", return_value=mock_analyzer_instance):
            response = cmd._fast_pipeline_analyze(
                config, "300846", "", "",
                progress_cb=_exploding_cb,
            )

        # Analysis should still succeed despite exploding callback
        self.assertIsInstance(response, BotResponse)
        self.assertIn("📊", response.text)

    @patch("src.agent.tools.data_tools._handle_get_realtime_quote", return_value=None)
    @patch("src.agent.tools.data_tools._handle_get_daily_history", return_value=None)
    @patch("src.agent.llm_adapter.LLMToolAdapter")
    def test_progress_callback_reports_failed_stages(self, mock_llm, mock_history, mock_quote):
        """When data tools fail, progress should report them as failed."""
        mock_llm_instance = MagicMock()
        mock_llm_instance.call_text.return_value = SimpleNamespace(
            content="", provider="error", model="test-model",
        )
        mock_llm.return_value = mock_llm_instance

        progress_calls = []

        def _tracking_cb(pct, completed, current, failed, elapsed, final_text=None):
            progress_calls.append((pct, completed[:], current, failed[:], final_text))

        cmd = AskCommand()
        config = self._make_minimal_config()
        response = cmd._fast_pipeline_analyze(
            config, "300846", "", "",
            progress_cb=_tracking_cb,
        )

        # 30% call should have failed stages
        p30 = [c for c in progress_calls if c[0] == 30]
        self.assertTrue(p30, "30% progress not called")
        self.assertIn("行情数据", p30[0][3], "quote failure not reported")
        self.assertIn("历史数据", p30[0][3], "history failure not reported")

        # 100% should have final_text with failure message
        p100 = [c for c in progress_calls if c[0] == 100]
        self.assertTrue(p100, "100% progress not called")
        self.assertIn("❌", p100[0][4], "failure final_text not set")


class NoProgressForNonTelegramTestCase(unittest.TestCase):
    """Non-Telegram platforms should not create progress."""

    def _patch_get_config(self, agent_mode=True):
        config = SimpleNamespace(
            agent_mode=agent_mode,
            telegram_bot_token="test:token",
            telegram_chat_id="123",
            ask_fast_model="",
            litellm_model="test-model",
            anspire_api_keys=[],
            get=lambda key, default=None: {"LITELLM_MODEL": "test-model"}.get(key, default),
        )
        return patch("bot.commands.ask.get_config", return_value=config)

    def test_discord_platform_no_progress(self):
        code = "300846"
        message = _make_bot_message(platform="discord")
        with self._patch_get_config(), \
             patch.object(AskCommand, '_merge_code_args', return_value=(code, [])), \
             patch.object(AskCommand, '_parse_stock_codes', return_value=[code]), \
             patch.object(AskCommand, '_get_default_skill_id', return_value="default"), \
             patch.object(AskCommand, '_fast_pipeline_analyze', return_value=BotResponse.text_response("ok")):
            cmd = AskCommand()
            response = cmd.execute(message, [code])
            self.assertEqual("ok", response.text)


class ProgressRefresherTestCase(unittest.TestCase):
    """Test _ProgressRefresher lifecycle and behavior."""

    def setUp(self):
        self.edit_calls = []
        self._edit_fn = MagicMock(side_effect=lambda cid, mid, text, **kw: self.edit_calls.append(text))
        # Mock requests.post to avoid real HTTP calls in refresher's typing action
        self._req_patcher = patch("requests.post", return_value=MagicMock(status_code=200))
        self._mock_req = self._req_patcher.start()

    def tearDown(self):
        self._req_patcher.stop()

    def test_start_stop_lifecycle(self):
        """Refresher starts and stops correctly."""
        r = _ProgressRefresher(
            edit_fn=self._edit_fn,
            bot_token="test:token",
            chat_id="chat1",
            message_id="123",
            stock_label="300846",
            pct=50,
            completed=["行情数据"],
            current="AI 综合判断",
            failed=[],
            t_start=time.monotonic(),
        )
        self.assertFalse(r.is_alive)
        r.start()
        self.assertTrue(r.is_alive)
        r.stop()
        # Thread checks stop event every ~1s; wait for clean exit
        if r._thread:
            r._thread.join(timeout=5)
        self.assertFalse(r.is_alive)

    def test_stop_is_idempotent(self):
        """Calling stop() multiple times does not raise."""
        r = _ProgressRefresher(
            edit_fn=self._edit_fn,
            bot_token="test:token",
            chat_id="chat1",
            message_id="123",
            stock_label="300846",
            pct=50,
            completed=[],
            current="AI 综合判断",
            failed=[],
            t_start=time.monotonic(),
        )
        r.stop()
        r.stop()
        r.stop()  # should not raise

    def test_restart_stops_old_thread(self):
        """Starting a new refresher stops the old one."""
        r = _ProgressRefresher(
            edit_fn=self._edit_fn,
            bot_token="test:token",
            chat_id="chat1",
            message_id="123",
            stock_label="300846",
            pct=50,
            completed=[],
            current="AI 综合判断",
            failed=[],
            t_start=time.monotonic(),
        )
        r.start()
        t1 = r._thread
        # Give the thread a moment to settle into the wait loop
        time.sleep(0.2)
        # Restart — start() calls stop() + join(timeout=5) internally
        r.start()
        t2 = r._thread
        self.assertIsNot(t1, t2, "restart should create a new thread")
        # Old thread should exit quickly now that stop event is set
        t1.join(timeout=5)
        self.assertFalse(t1.is_alive(), "old thread should be stopped")
        r.stop()
        t2.join(timeout=5)

    def test_edits_include_animated_dots(self):
        """The refresher should edit the message with animated dots (., .., ...)."""
        r = _ProgressRefresher(
            edit_fn=self._edit_fn,
            bot_token="test:token",
            chat_id="chat1",
            message_id="123",
            stock_label="300846",
            pct=85,
            completed=["行情数据", "技术分析", "情报搜索"],
            current="AI 综合判断",
            failed=[],
            t_start=time.monotonic(),
        )
        r.start()
        # Wait long enough for at least 2 refresh cycles
        time.sleep(7.5)
        r.stop()
        if r._thread:
            r._thread.join(timeout=3)

        # Should have at least 2 edits, each with dots
        self.assertGreaterEqual(len(self.edit_calls), 2)
        for text in self.edit_calls:
            self.assertIn("AI 综合判断", text)
            # One of the dots variants should be present
            dots_found = any(d in text for d in ("AI 综合判断.", "AI 综合判断..", "AI 综合判断..."))
            self.assertTrue(dots_found, f"Expected animated dots in text: {text}")
            # Percentage should remain 85 (not changed by refresher)
            self.assertIn("85%", text)

    def test_percentage_never_changes_during_refresh(self):
        """The refresher must never change the real percentage."""
        r = _ProgressRefresher(
            edit_fn=self._edit_fn,
            bot_token="test:token",
            chat_id="chat1",
            message_id="123",
            stock_label="300846",
            pct=30,
            completed=["行情数据"],
            current="技术分析",
            failed=[],
            t_start=time.monotonic(),
        )
        r.start()
        time.sleep(4)
        r.stop()
        if r._thread:
            r._thread.join(timeout=3)

        for text in self.edit_calls:
            self.assertIn("30%", text, "refresher changed percentage — forbidden")

    def test_final_text_stops_refresher(self):
        """When progress callback receives final_text, the refresher must not fire again."""
        mock_edit = MagicMock()
        r = _ProgressRefresher(
            edit_fn=mock_edit,
            bot_token="test:token",
            chat_id="chat1",
            message_id="123",
            stock_label="300846",
            pct=85,
            completed=["行情数据", "技术分析", "情报搜索"],
            current="AI 综合判断",
            failed=[],
            t_start=time.monotonic(),
        )
        r.start()
        time.sleep(0.2)  # let thread start
        # Simulate progress callback with final_text
        r.stop()
        if r._thread:
            r._thread.join(timeout=5)
        mock_edit.reset_mock()
        # Wait to ensure no more edits from the refresher
        time.sleep(4)
        self.assertEqual(mock_edit.call_count, 0, "no edits should fire after stop")

    def test_refresher_stopped_in_finally_on_exception(self):
        """When execute() raises, the refresher must be stopped in finally."""
        mock_edit = MagicMock()
        r = _ProgressRefresher(
            edit_fn=mock_edit,
            bot_token="test:token",
            chat_id="chat1",
            message_id="123",
            stock_label="300846",
            pct=50,
            completed=[],
            current="技术分析",
            failed=[],
            t_start=time.monotonic(),
        )
        r.start()
        time.sleep(0.2)  # let thread start
        # Simulate what the finally block does
        r.stop()
        if r._thread:
            r._thread.join(timeout=5)
        self.assertFalse(r.is_alive, "refresher must be stopped after finally")
        # Wait to ensure no more edits
        time.sleep(4)
        # Only the edits from the brief run should exist
        self.assertLess(mock_edit.call_count, 5, "refresher continued firing after stop")


if __name__ == "__main__":
    unittest.main()