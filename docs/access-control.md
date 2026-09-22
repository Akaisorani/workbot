# WeLink ingestion, reply and execution policy (V1.2)

V1.2 separates three questions that older `access_control` combined:

1. **Ingestion Policy** — may WorkBot read/store this conversation message?
2. **Reply Policy** — may WorkBot answer this sender/conversation using read-only information capabilities?
3. **Execution Policy** — may this sender/conversation create or control execution, mutate state, or approve actions?

All policies use the same precedence: sender/group denylist wins, then sender/group allowlist, then `default`.

Recommended personal-office configuration:

```json
"ingestion_policy": {
  "default": "allow",
  "deny_senders": [],
  "deny_groups": []
},
"reply_policy": {
  "default": "allow",
  "deny_senders": [],
  "deny_groups": [],
  "silent_if_unfulfillable": true
},
"execution_policy": {
  "default": "deny",
  "allow_senders": ["YOUR_WELINK_ACCOUNT"],
  "allow_groups": []
}
```

With `reply_policy.default=allow`, an empty `allow_senders` means “allow everyone except explicit deny entries”; it does **not** mean “allow nobody”.

## Reply Policy is read-only work Q&A, not control-plane visibility

A reply-only user may ask WorkBot to answer from existing knowledge or read/search sources such as files, web, Wiki or mail when configured. A single read-only Agent turn first decides whether WorkBot can materially solve the request. If it cannot, or the request needs commands/program execution/file modification/task creation/side effects, WorkBot stays silent when `silent_if_unfulfillable=true`.

Reply Policy does not grant access to WorkBot's private control plane. Commands such as `/nodes`, `/status`, `/session`, `/memories`, `/welink`, task/workflow controls and approvals require Execution Policy.

## Execution Policy

Execution Policy covers task/workflow creation and control, running commands/programs on behalf of the requester, file/code mutation, memory/control-plane mutation, and approval authority. Action Policy still decides whether a particular high-risk action is auto-allowed, requires approval, or is denied after the requester has execution authority.

## Legacy `access_control`

For compatibility, if an explicit policy block is absent, WorkBot may fall back to `access_control`. New deployments should start from `config/workbot.example.json` and configure the explicit policy sections directly.


## V1.11.1 private operator identity

Execution policy continues to be sender-based and does not inherently distinguish group vs private conversations. For manual local DMs, WeLink Hook/history fields can expose the sender account inconsistently. V1.11.1 treats `isMine=true` as authoritative for a local manual DM and normalizes it to the single configured `im.self_accounts` account. If `im.self_accounts` is empty and `execution_policy.allow_senders` contains exactly one account, WorkBot safely infers that account as the local self identity. This fallback is only applied to `conversation_kind=user AND from_self=true`; inbound peer DMs never receive operator execution permission.
