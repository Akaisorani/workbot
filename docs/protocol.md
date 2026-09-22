# WorkBot protocol V1

Transport is newline-delimited JSON (NDJSON). Every logical message is one JSON object per line.

Message types: `request`, `response`, `event`, `ack`; `heartbeat` remains reserved.

## Durable events

The Linux worker inserts an event into its SQLite outbox before upstream delivery. WorkBot persists the event before ACK. Event IDs are unique, so reconnect retransmission is idempotent.

## Windows -> Linux request methods

- `task.create`: start a generic CodeAgent task;
- `task.status`: read worker-side task state;
- `task.cancel`: cancel a non-terminal task and terminate its tmux session when present;
- `ping`.

## Linux -> Windows request methods (V0.7)

Linux-local clients submit a `request` to the Unix socket. The worker forwards it over the existing upstream and keeps the local connection associated with the request ID until the matching `response` arrives.

Allow-listed Windows methods:

- `windows.fs.read`;
- `windows.fs.list`;
- `workbot.query`.

These are synchronous, bounded and read-only in V0.7. If no Windows upstream is connected, the worker immediately returns `WorkBotUnavailable`. If the upstream disconnects mid-request, it returns `WorkBotDisconnected` rather than waiting indefinitely.

## Task semantics

`task.create.data.instruction` is authoritative. WorkBot does not rewrite it into a fixed development/test/build taxonomy. `conversation_id` is also attached so a managed Linux Agent can issue a later RPC with the originating Conversation context.

## Events

- `task.started`: routine start;
- `task.progress`: optional Agent-selected milestone;
- `task.blocked`: optional blocker requiring attention;
- `task.completed`: authoritative terminal success;
- `task.failed`: authoritative terminal failure;
- `task.cancelled`: authoritative terminal cancellation;
- arbitrary local events from manually started Agents via `agentctl notify`.

Workflow child terminal events wake the orchestrator and are not duplicated as standalone IM completion messages.

## V0.8 policy request

Linux-local `agentctl request-approval` sends a normal synchronous request through the worker/upstream multiplexing layer:

```json
{
  "type": "request",
  "method": "policy.request_approval",
  "task_id": "task-...",
  "data": {
    "action": "git.push",
    "summary": "Push validated fix to origin/main",
    "details": {"remote": "origin", "branch": "main"},
    "conversation_id": "welink:group:...",
    "timeout_seconds": 900
  }
}
```

The final response contains:

```json
{
  "ok": true,
  "result": {
    "approval_id": "approval-...",
    "decision": "approved|denied|expired|interrupted",
    "approved": true,
    "auto": false
  }
}
```

For policy `allow`/`deny`, WorkBot may respond immediately without creating an approval ID. An `approve` decision creates a durable audit row and the request remains open while the user decides in WeLink.


## V0.9 recovery/audit events

Linux final task events use deterministic IDs so worker restart/recovery remains idempotent. `policy.action.executed` is a durable audit event with action, approval id, actual argv/cwd and exit code; WorkBot stores/ACKs it but does not forward it to IM as routine progress.
