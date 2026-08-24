# -*- coding: utf-8 -*-
"""Unit tests for Telegram Long Polling client (bot/platforms/telegram_polling.py).

Covers message parsing → BotMessage, proxy building, getUpdates offset semantics,
and the config-switch guard for starting the background poller. Network is mocked
out; no real Telegram calls happen.
"""

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from bot.models import ChatType
from bot.platforms.telegram_polling import (
    TelegramPollingClient,
    _build_proxies,
)


def _client(**config) -> TelegramPollingClient:
    defaults = {
        "telegram_bot_token": "TESTTOKEN",
        "http_proxy": None,
        "https_proxy": None,
    }
    defaults.update(config)
    return TelegramPollingClient(SimpleNamespace(**defaults))


class TestBuildProxies(unittest.TestCase):
    def test_no_proxy_returns_none(self):
        cfg = SimpleNamespace(http_proxy=None, https_proxy=None)
        self.assertIsNone(_build_proxies(cfg))

    def test_http_proxy_only(self):
        cfg = SimpleNamespace(http_proxy="http://127.0.0.1:10809", https_proxy=None)
        self.assertEqual(_build_proxies(cfg), {"http": "http://127.0.0.1:10809"})

    def test_both_proxies(self):
        cfg = SimpleNamespace(
            http_proxy="http://127.0.0.1:10809",
            https_proxy="socks5h://127.0.0.1:1080",
        )
        self.assertEqual(
            _build_proxies(cfg),
            {"http": "http://127.0.0.1:10809", "https": "socks5h://127.0.0.1:1080"},
        )


class TestParseMessage(unittest.TestCase):
    def test_private_text_message(self):
        client = _client()
        update = {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 111, "username": "alice", "first_name": "Alice"},
                "chat": {"id": 222, "type": "private"},
                "text": "/analyze 600519",
                "date": 1700000000,
            },
        }
        msg = client.parse_message(update)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.platform, "telegram")
        self.assertEqual(msg.chat_type, ChatType.PRIVATE)
        self.assertEqual(msg.user_id, "111")
        self.assertEqual(msg.chat_id, "222")
        self.assertEqual(msg.user_name, "alice")
        self.assertEqual(msg.content, "/analyze 600519")
        self.assertFalse(msg.mentioned)

    def test_group_message_strips_mention_and_sets_mentioned(self):
        client = _client()
        client._bot_username = "MyStockBot"
        update = {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "from": {"id": 333, "first_name": "Bob"},
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "@MyStockBot /help",
                "date": 1700000000,
            },
        }
        msg = client.parse_message(update)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.chat_type, ChatType.GROUP)
        self.assertTrue(msg.mentioned)
        self.assertEqual(msg.content, "/help")

    def test_non_message_update_returns_none(self):
        client = _client()
        self.assertIsNone(client.parse_message({"update_id": 3, "callback_query": {}}))
        self.assertIsNone(client.parse_message({}))
        self.assertIsNone(client.parse_message("not-a-dict"))

    def test_empty_text_returns_none(self):
        client = _client()
        update = {
            "update_id": 4,
            "message": {
                "message_id": 12,
                "from": {"id": 111},
                "chat": {"id": 222, "type": "private"},
                "text": "   ",
            },
        }
        self.assertIsNone(client.parse_message(update))

    def test_missing_user_or_chat_returns_none(self):
        client = _client()
        update = {
            "update_id": 5,
            "message": {"message_id": 13, "chat": {"id": 222, "type": "private"}, "text": "hi"},
        }
        self.assertIsNone(client.parse_message(update))

    def test_group_command_with_bot_suffix_stripped(self):
        """/batch@MyStockBot 应剥离 @bot 后缀，保留 /batch 交给命令解析。"""
        client = _client()
        client._bot_username = "MyStockBot"
        update = {
            "update_id": 6,
            "message": {
                "message_id": 20,
                "from": {"id": 444, "username": "carol"},
                "chat": {"id": -1002, "type": "supergroup"},
                "text": "/batch@MyStockBot",
                "date": 1700000000,
            },
        }
        msg = client.parse_message(update)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, "/batch")
        # 群聊命令后缀本身不应算作 @bot 提及
        self.assertFalse(msg.mentioned)

    def test_group_command_with_bot_suffix_and_args(self):
        client = _client()
        client._bot_username = "MyStockBot"
        update = {
            "update_id": 7,
            "message": {
                "message_id": 21,
                "from": {"id": 444, "username": "carol"},
                "chat": {"id": -1002, "type": "supergroup"},
                "text": "/batch@MyStockBot 600519,000858",
                "date": 1700000000,
            },
        }
        msg = client.parse_message(update)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, "/batch 600519,000858")

    def test_private_command_slash_preserved(self):
        """私聊斜杠命令保持原样，前缀剥离由 get_command_and_args 负责。"""
        client = _client()
        update = {
            "update_id": 8,
            "message": {
                "message_id": 22,
                "from": {"id": 111, "username": "alice"},
                "chat": {"id": 222, "type": "private"},
                "text": "/status",
                "date": 1700000000,
            },
        }
        msg = client.parse_message(update)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.content, "/status")
        self.assertEqual(msg.get_command_and_args("/"), ("status", []))


class TestGetUpdates(unittest.TestCase):
    def test_no_offset_when_unset(self):
        client = _client()
        client._request = mock.Mock(return_value=[])
        client._offset = None
        client._get_updates()
        args, kwargs = client._request.call_args
        self.assertEqual(args[0], "getUpdates")
        self.assertEqual(kwargs["timeout"], 50)
        self.assertEqual(kwargs["allowed_updates"], '["message"]')
        self.assertNotIn("offset", kwargs)

    def test_offset_passed_when_set(self):
        client = _client()
        client._request = mock.Mock(return_value=[])
        client._offset = 100
        client._get_updates()
        kwargs = client._request.call_args[1]
        self.assertEqual(kwargs["offset"], 100)


class TestStartGuard(unittest.TestCase):
    def test_missing_token_skips(self):
        with mock.patch("src.config.get_config",
                        return_value=SimpleNamespace(telegram_bot_token=None,
                                                     telegram_polling_enabled=True)):
            from bot.platforms.telegram_polling import start_telegram_polling_background, stop_telegram_polling
            stop_telegram_polling()
            self.assertFalse(start_telegram_polling_background())

    def test_disabled_by_switch_skips(self):
        with mock.patch("src.config.get_config",
                        return_value=SimpleNamespace(telegram_bot_token="T",
                                                     telegram_polling_enabled=False)):
            from bot.platforms.telegram_polling import start_telegram_polling_background, stop_telegram_polling
            stop_telegram_polling()
            self.assertFalse(start_telegram_polling_background())


if __name__ == "__main__":
    unittest.main()