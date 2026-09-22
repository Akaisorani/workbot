import unittest
from workbot.conversation.models import IncomingMessage
from workbot.policy.engine import PolicyEngine


def msg(sender="user1", group="g1"):
    return IncomingMessage(
        platform="welink", conversation_id=f"welink:group:{group}", conversation_kind="group",
        external_conversation_id=group, external_message_id="1", sender_id=sender,
        content="hello", sent_at_ms=1, display_name="g",
    )


class PolicyTests(unittest.TestCase):
    def test_default_deny_allow_sender(self):
        p = PolicyEngine({"default": "deny", "allow_senders": ["owner"]})
        self.assertTrue(p.inbound(msg("owner")).allowed)
        self.assertFalse(p.inbound(msg("other")).allowed)

    def test_blacklist_overrides_allowlist(self):
        p = PolicyEngine({
            "default": "deny", "allow_senders": ["owner"], "deny_senders": ["owner"],
            "allow_groups": ["g1"],
        })
        self.assertFalse(p.inbound(msg("owner", "g1")).allowed)

    def test_group_allowlist(self):
        p = PolicyEngine({"default": "deny", "allow_groups": ["g1"]})
        self.assertTrue(p.inbound(msg("other", "g1")).allowed)
        self.assertFalse(p.inbound(msg("other", "g2")).allowed)


if __name__ == "__main__":
    unittest.main()
