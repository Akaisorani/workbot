from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Iterable

from workbot.conversation.models import IncomingMessage


@dataclass(slots=True)
class PolicyDecision:
    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""
    decision: str = "allow"  # allow | approve | deny
    matched_rule: str | None = None


class PolicyEngine:
    """Inbound IM ACL plus configurable action policy.

    Inbound ACL precedence:
      1. explicit sender/group deny wins;
      2. explicit sender/group allow wins;
      3. ``default`` decides.

    Action policy uses first-match glob rules.  It is intentionally a control-
    plane policy: it governs WorkBot APIs/agentctl approval flow, but it is not
    an OS/kernel sandbox for arbitrary shell commands executed by CodeAgent.
    """

    LEGACY_LOW_RISK = {"read", "search", "status"}
    LEGACY_HIGH_RISK = {"push", "merge", "deploy", "delete", "sudo", "send_external"}

    DEFAULT_ACTION_RULES = [
        {"match": [
            "read.*", "search.*", "status.*", "windows.fs.read", "windows.fs.list", "workbot.query",
            "code.edit.workspace", "build.run", "test.run", "git.status", "git.diff", "git.commit.local",
        ], "decision": "allow"},
        {"match": [
            "git.push", "git.merge", "git.force_push", "git.reset_hard", "git.clean.force", "deploy.*", "release.*",
            "im.send", "im.write", "email.send", "email.write", "meeting.write", "calendar.write",
            "cloud.write", "cloud.share", "contact.write",
            "fs.delete.*", "fs.write.outside_workspace", "database.write.*", "process.sudo",
        ], "decision": "approve"},
        {"match": [
            "credential.export", "secret.exfiltrate", "security.disable", "system.destructive",
        ], "decision": "deny"},
    ]

    def __init__(self, cfg: dict | None = None, action_cfg: dict | None = None):
        cfg = cfg or {}
        self.default = str(cfg.get("default", "allow")).lower()
        if self.default not in {"allow", "deny"}:
            raise ValueError("access_control.default must be 'allow' or 'deny'")
        self.allow_senders = self._set(cfg.get("allow_senders", []))
        self.deny_senders = self._set(cfg.get("deny_senders", []))
        self.allow_groups = self._set(cfg.get("allow_groups", []))
        self.deny_groups = self._set(cfg.get("deny_groups", []))
        self.log_denied = bool(cfg.get("log_denied", True))
        self.reply_denied = bool(cfg.get("reply_denied", False))

        action_cfg = action_cfg or {}
        self.action_default = str(action_cfg.get("default", "approve")).lower()
        if self.action_default not in {"allow", "approve", "deny"}:
            raise ValueError("action_policy.default must be allow/approve/deny")
        self.action_rules = action_cfg.get("rules") or self.DEFAULT_ACTION_RULES

    @staticmethod
    def _set(values: Iterable[object]) -> set[str]:
        return {str(v) for v in values if str(v)}

    def inbound(self, msg: IncomingMessage) -> PolicyDecision:
        sender = str(msg.sender_id or "")
        group = str(msg.external_conversation_id or "") if msg.conversation_kind == "group" else ""
        if sender in self.deny_senders:
            return PolicyDecision(False, reason=f"sender {sender} is denylisted", decision="deny")
        if group and group in self.deny_groups:
            return PolicyDecision(False, reason=f"group {group} is denylisted", decision="deny")
        if sender in self.allow_senders:
            return PolicyDecision(True, reason="sender allowlisted", decision="allow")
        if group and group in self.allow_groups:
            return PolicyDecision(True, reason="group allowlisted", decision="allow")
        allowed = self.default == "allow"
        return PolicyDecision(allowed, reason=f"default={self.default}", decision="allow" if allowed else "deny")

    def evaluate_action(self, action: str) -> PolicyDecision:
        action = str(action or "").strip().lower()
        if not action:
            return PolicyDecision(False, True, "empty/unknown action requires approval", "approve")
        for i, rule in enumerate(self.action_rules):
            patterns = rule.get("match", []) if isinstance(rule, dict) else []
            if isinstance(patterns, str):
                patterns = [patterns]
            if any(fnmatch.fnmatchcase(action, str(pattern).lower()) for pattern in patterns):
                decision = str(rule.get("decision", "approve")).lower()
                if decision not in {"allow", "approve", "deny"}:
                    decision = "approve"
                return PolicyDecision(
                    allowed=decision == "allow",
                    requires_confirmation=decision == "approve",
                    reason=str(rule.get("reason") or f"matched action rule {i+1}"),
                    decision=decision,
                    matched_rule=",".join(str(x) for x in patterns),
                )
        d = self.action_default
        return PolicyDecision(
            allowed=d == "allow", requires_confirmation=d == "approve",
            reason=f"action default={d}", decision=d,
        )

    def evaluate(self, action: str) -> PolicyDecision:
        """Backward-compatible legacy action evaluator used by older tests/code."""
        if action in self.LEGACY_LOW_RISK:
            return PolicyDecision(True, False, "low-risk action", "allow")
        if action in self.LEGACY_HIGH_RISK:
            return PolicyDecision(False, True, "explicit approval required", "approve")
        return self.evaluate_action(action)
