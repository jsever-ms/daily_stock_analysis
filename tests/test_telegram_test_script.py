# -*- coding: utf-8 -*-
"""回归测试：Telegram 快速测试脚本（scripts/test_telegram.py）的最小依赖约束。

核心断言：
1. 脚本链路绝不导入 src.config / src.notification_sender 包级接口
   （即不会连带加载 EmailSender、Feishu、data_provider、股票行情等重型模块）；
2. getMe 状态先于 sendMessage 状态输出；
3. 完整 Token 不进入输出；
4. 成功路径返回 0，401 路径返回 1 且不调用 sendMessage。
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = __file__.rsplit("/", 2)[0]
sys.path.insert(0, ROOT)

import scripts.test_telegram as test_script  # noqa: E402


def _block_heavy_imports():
    """在测试期间屏蔽重型导入链，模拟最小依赖环境。

    `import X` 遇到 sys.modules 中为 None 的条目会直接抛 ImportError，
    任一环节意外尝试加载 src.config / src.notification_sender 都会导致测试失败。
    """
    blocked = {}
    for name in ("src.config", "src.notification_sender"):
        if name in sys.modules:
            blocked[name] = sys.modules[name]
        sys.modules[name] = None
    return blocked


def _restore_imports(blocked):
    for name, module in blocked.items():
        sys.modules[name] = module
    for name in ("src.config", "src.notification_sender"):
        if sys.modules.get(name) is None:
            del sys.modules[name]


class TestTelegramTestScript(unittest.TestCase):

    def setUp(self):
        self._env_backup = {}
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DSA_GIT_COMMIT"):
            self._env_backup[key] = os.environ.get(key)
        self._blocked = _block_heavy_imports()

    def tearDown(self):
        _restore_imports(self._blocked)
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _set_env(self, token="123456789:REALREALREALREAL", chat_id="-1001234567890"):
        os.environ["TELEGRAM_BOT_TOKEN"] = token
        os.environ["TELEGRAM_CHAT_ID"] = chat_id
        os.environ["DSA_GIT_COMMIT"] = "deadbeef"

    def _run_main(self):
        """运行脚本 main，捕获 stdout。"""
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = test_script.main()
        return rc, buf.getvalue()

    def test_success_path_minimal_deps_and_order(self):
        """最小依赖（屏蔽 src.config 导入链）+ 成功路径：
        getMe 状态先输出，再输出 sendMessage 状态；完整 Token 不泄露。"""
        self._set_env()
        with mock.patch("requests.get") as mock_get, \
             mock.patch("requests.post") as mock_post:
            mock_get.return_value = SimpleNamespace(
                status_code=200,
                json=lambda: {"ok": True, "result": {"username": "test_bot"}},
                text="",
            )
            mock_post.return_value = SimpleNamespace(
                status_code=200, json=lambda: {"ok": True}, text="",
            )
            rc, output = self._run_main()

        # 屏蔽 src.config 后仍能跑通：证明脚本未依赖重型导入链
        self.assertEqual(rc, 0)
        self.assertIn("getMe: 成功 (HTTP 200)", output)
        self.assertIn("sendMessage: 成功 (HTTP 200)", output)
        self.assertLess(output.index("getMe:"), output.index("sendMessage:"))
        self.assertIn("✅ Telegram 推送测试成功", output)
        # 诊断行存在且完整 Token 不出现
        self.assertIn("Telegram token: present=True", output)
        self.assertNotIn("REALREALREALREAL", output)
        # 发送给 Telegram 的 payload 包含固定文案 / 时间 / 环境 / commit
        sent_text = mock_post.call_args.kwargs["json"]["text"]
        self.assertIn("✅ Telegram 推送测试成功", sent_text)
        self.assertIn("git commit: deadbeef", sent_text)

    def test_401_returns_failure_and_blocks_send(self):
        """getMe 401：脚本返回 1，明确提示 Token 无效，且不调用 sendMessage。"""
        self._set_env()
        with mock.patch("requests.get") as mock_get, \
             mock.patch("requests.post") as mock_post:
            mock_get.return_value = SimpleNamespace(
                status_code=401,
                json=lambda: {"ok": False, "error_code": 401, "description": "Unauthorized"},
                text="Unauthorized",
            )
            rc, output = self._run_main()

        self.assertEqual(rc, 1)
        self.assertIn("getMe: Token 无效或未正确加载 (HTTP 401)", output)
        self.assertIn("❌ Token 无效", output)
        mock_post.assert_not_called()

    def test_missing_chat_id_reports_clearly(self):
        """缺少 Chat ID：getMe 成功后明确提示 TELEGRAM_CHAT_ID 未配置。"""
        self._set_env(chat_id="")
        with mock.patch("requests.get") as mock_get, \
             mock.patch("requests.post") as mock_post:
            mock_get.return_value = SimpleNamespace(
                status_code=200,
                json=lambda: {"ok": True, "result": {"username": "test_bot"}},
                text="",
            )
            rc, output = self._run_main()

        self.assertEqual(rc, 1)
        self.assertIn("TELEGRAM_CHAT_ID 未配置", output)
        mock_post.assert_not_called()

    def test_missing_token_reports_clearly(self):
        """缺少 Token：直接提示 TELEGRAM_BOT_TOKEN 未配置，不发起任何请求。"""
        self._set_env(token="")
        with mock.patch("requests.get") as mock_get, \
             mock.patch("requests.post") as mock_post:
            rc, output = self._run_main()

        self.assertEqual(rc, 1)
        self.assertIn("TELEGRAM_BOT_TOKEN 未配置", output)
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    def test_token_with_space_is_rejected_early(self):
        """Token 含空格（Secret 粘贴事故）：提前拒绝，不发起请求。"""
        self._set_env(token="123456789: REALREALREALREAL")
        with mock.patch("requests.get") as mock_get, \
             mock.patch("requests.post") as mock_post:
            rc, output = self._run_main()

        self.assertEqual(rc, 1)
        self.assertIn("疑似包含多余空格", output)
        self.assertIn("space=True", output)
        mock_get.assert_not_called()
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()