# WeLink full-conversation ingestion and intent routing (V1.2-V1.5)

## V1.5 receive path

When `im.realtime.enabled=true`, WeLinkBot WebSocket push is the primary receive path. The discovery/history mechanism described below remains active as startup/reconnect backfill, periodic consistency reconciliation and automatic fallback while the WebSocket is unavailable. See `docs/realtime-welink.md`.


## Discovery

When `im.discovery.mode="all"`, WorkBot periodically calls `welink-cli im query-recent-conversation` and dynamically tracks recently visible group and private conversations. The old `im.groups` list remains a static seed/compatibility list; it no longer limits ingestion in discovery mode.

WorkBot keeps a last-message cursor per conversation and adaptively polls active conversations more frequently than quiet ones. `bootstrap_from_latest=true` avoids replaying an entire historical chat when a conversation is first discovered.

## Ingestion is not Agent invocation

Every ingested message may be persisted for conversation context, but most group-chat messages must never consume CodeAgent inference or trigger a reply.

Routing order:

```text
message
  -> ingestion policy
  -> durable Conversation Store
  -> self/manual-DM? store only
  -> direct @Bot / slash control / pending confirmation? immediate
  -> private DM? immediate read-only/full policy handling
  -> ordinary group chatter
       -> cheap lexical filter
       -> delayed batch
       -> one conservative low-priority intent-classifier call
       -> only selected messages enter the Conversation Agent queue
```

Direct mentions such as `@WorkBot` (or configured aliases) are high priority. Important work groups use a shorter batching delay. Large/noisy groups are delayed and batched; if the global CodeAgent scheduler is already backed up, an ambient classification batch may be skipped rather than consuming scarce inference capacity. Messages remain stored even when classification is skipped.

## Private-message behavior

A private message covered by Reply Policy does not automatically receive a generic Bot response. WorkBot uses one read-only Agent call to decide whether it can actually solve the request. If `can_reply=false`, confidence is below threshold, or the task requires execution without Execution Policy, WorkBot remains silent so the operator can answer manually.

Manual messages sent by the operator in a private conversation are stored as `SELF` context and are never auto-replied to by WorkBot.

## CodeAgent budget

V1.2 has a global priority scheduler. Default priorities:

- direct/interactive user request: `0`
- workflow/planning/RPC: `10`
- ambient group intent batch: `40`
- summary/memory/global-memory maintenance: `80`

Running calls are not preempted. The scheduler controls which waiting call receives the next slot. `agent.max_concurrent` defaults to 2 and should be kept conservative when inference capacity is constrained.
