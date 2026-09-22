import unittest
from workbot.im.welink import WeLinkAdapter


class FakeWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__("welink-cli", [{"group_id": "1001", "group_name": "Example Group"}],
                         bootstrap_from_latest=False)
        self.payload = {}

    def _run_json(self, *args):
        return self.payload


class WeLinkTests(unittest.TestCase):
    def test_parse_given_history_shape_and_ignore_bot_reply(self):
        a = FakeWeLink()
        a.payload = {
            "respData": {
                "chatInfo": [
                    {"content": "这是一条测试消息", "contentType": "TEXT_MSG", "groupId": 1001,
                     "msgId": 111, "sender": "example-user-b", "serverSendTime": 1001},
                    {"content": "[自动回复]已收到", "contentType": "TEXT_MSG", "groupId": 1001,
                     "msgId": 112, "sender": "example-user-b", "serverSendTime": 1002},
                ]
            }
        }
        msgs = a._poll_sync()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].content, "这是一条测试消息")
        self.assertEqual(msgs[0].sender_id, "example-user-b")

    def test_ignore_wrapped_bot_reply(self):
        a = FakeWeLink()
        a.payload = {
            "respData": {"chatInfo": [
                {"content": '{"text":"[自动回复]CodeAgent 调用失败：x"}', "contentType": "TEXT_MSG",
                 "msgId": 10, "sender": "example-user-b", "serverSendTime": 10}
            ]}
        }
        self.assertEqual(a._poll_sync(), [])

    def test_ignore_recent_outgoing_even_if_prefix_missing(self):
        a = FakeWeLink()
        a._remember_outgoing("1001", "回执内容")
        a.payload = {
            "respData": {"chatInfo": [
                {"content": "回执内容", "contentType": "TEXT_MSG", "msgId": 11,
                 "sender": "example-user-b", "serverSendTime": 11}
            ]}
        }
        self.assertEqual(a._poll_sync(), [])


if __name__ == "__main__":
    unittest.main()


class TransientWeLink(WeLinkAdapter):
    def __init__(self):
        super().__init__("welink-cli", [{"group_id":"1","group_name":"g"}],
                         bootstrap_from_latest=False, transient_backoff_seconds=10)
        self.calls = 0
    def _run_json(self, *args):
        self.calls += 1
        raise RuntimeError("welink-cli failed (1): [ERROR] WebSocket: Verification timeout\nError: WeLink PC verification timeout.")


def test_verification_timeout_enters_backoff_without_raising():
    a = TransientWeLink()
    assert a._poll_sync() == []
    assert a.calls == 1
    assert a._poll_backoff_until > 0
    assert a._poll_sync() == []
    assert a.calls == 1

import asyncio
import time
from workbot.im.welink import WeLinkTransientError


class AmbiguousSendWeLink(FakeWeLink):
    def __init__(self, delivered: bool):
        super().__init__()
        self.delivered = delivered
        self.send_calls = 0
        self._send_verify_delay_seconds = 0

    def _send_group_once(self, group_id, text):
        self.send_calls += 1
        raise WeLinkTransientError("WebSocket: Verification timeout")

    def _history_contains(self, group_id, full_text, *, created_at_ms):
        return self.delivered


def test_ambiguous_send_that_history_confirms_is_success():
    a = AmbiguousSendWeLink(True)
    asyncio.run(a.send_text("welink:group:1001", "hello", created_at_ms=int(time.time()*1000)))
    assert a.send_calls == 1
