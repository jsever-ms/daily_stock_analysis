# -*- coding: utf-8 -*-
"""Tests for the final command-passthrough guard in the LLM adapter.

The guard lives in ``LLMToolAdapter.call_completion`` and is the last safety
net before any LLM API call: if the latest user turn starts with ``/`` it is
refused (and logged), so a regression in the Command Router can never again
let a Telegram command leak into the chat model.
"""

import sys
import unittest

sys.path.insert(0, __file__.rsplit("/", 2)[0])

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    from tests.litellm_stub import ensure_litellm_stub
    ensure_litellm_stub()

from src.agent.llm_adapter import LLMToolAdapter


class TestFindBlockedCommand(unittest.TestCase):
    """Unit coverage for the raw-text detection helper."""

    def setUp(self):
        self.adapter = LLMToolAdapter.__new__(LLMToolAdapter)

    def test_blocks_latest_user_slash_message(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "/status"},
        ]
        self.assertEqual(self.adapter._find_blocked_command(messages), "/status")

    def test_blocks_after_prior_history(self):
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "正常问题"},
            {"role": "assistant", "content": "回答"},
            {"role": "user", "content": "  /batch  "},
        ]
        self.assertEqual(self.adapter._find_blocked_command(messages), "/batch")

    def test_allows_normal_user_message(self):
        messages = [{"role": "user", "content": "贵州茅台现在怎么样？"}]
        self.assertIsNone(self.adapter._find_blocked_command(messages))

    def test_allows_tool_result_trailing(self):
        # Agent 循环中最后一条是 tool 结果：不应把历史用户消息误判为命令
        messages = [
            {"role": "user", "content": "分析一下 600519"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "content": "data"},
        ]
        self.assertIsNone(self.adapter._find_blocked_command(messages))

    def test_allows_empty_or_system_only(self):
        self.assertIsNone(self.adapter._find_blocked_command([]))
        self.assertIsNone(self.adapter._find_blocked_command([{"role": "system", "content": "x"}]))

    def test_allows_multimodal_text_block(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "正常问题"}, {"type": "image"}]}
        ]
        self.assertIsNone(self.adapter._find_blocked_command(messages))

    def test_blocks_multimodal_text_block_starting_slash(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "/help"}, {"type": "image"}]}
        ]
        self.assertEqual(self.adapter._find_blocked_command(messages), "/help")


class TestCallCompletionBlocksCommand(unittest.TestCase):
    """Integration: the guard triggers before any config/LLM access."""

    def test_call_completion_returns_error_for_slash_command(self):
        # __new__ 构造即可：守卫在任何 config 访问之前提前返回
        adapter = LLMToolAdapter.__new__(LLMToolAdapter)
        result = adapter.call_completion([{"role": "user", "content": "/status"}])
        self.assertEqual(result.provider, "error")
        self.assertIn("命令", result.content)

    def test_call_completion_logs_blocked_passthrough(self):
        adapter = LLMToolAdapter.__new__(LLMToolAdapter)
        with self.assertLogs("src.agent.llm_adapter", level="WARNING") as logs:
            adapter.call_completion([{"role": "user", "content": "/unknowncmd"}])
        self.assertTrue(
            any("Blocked command passthrough: /unknowncmd" in line for line in logs.output),
            logs.output,
        )


if __name__ == "__main__":
    unittest.main()
