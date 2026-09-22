# WorkBot action policy (V0.8)

Use this skill whenever a Windows-side Agent is about to perform a meaningful external/destructive side effect.

Normal engineering operations such as read/search, workspace edits, build/test, git status/diff and local commit are usually low risk. Before push/merge/force-push, deploy/release, sending IM/email, destructive/bulk deletion, writing outside the authorized workspace, production writes, or privilege escalation, stop before the action and emit:

```text
<WORKBOT_APPROVAL>{"action":"git.push","summary":"Push validated parser fix to origin/main","details":{"remote":"origin","branch":"main"}}</WORKBOT_APPROVAL>
```

Do not execute the sensitive action in the same turn. WorkBot will evaluate the configured policy. An allow rule may auto-resume; an approve rule asks the operator in WeLink; a deny rule stops the action. If resumed after approval and another distinct sensitive action becomes necessary, request approval again.

This control-plane protocol must not be bypassed merely because the current OS account can execute the command.
