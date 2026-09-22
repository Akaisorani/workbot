# WeLink IM reliability

`welink-cli` may occasionally report `WebSocket: Verification timeout` / `WeLink PC verification timeout` even while the PC client remains logged in. WorkBot treats this as a temporary/ambiguous CLI↔PC-helper transport problem. It does not automatically run `auth login` for every timeout.

Outbound replies use a Windows SQLite outbox. A reply is persisted before send. If send returns an ambiguous timeout, WorkBot queries recent group history for the exact normalized reply after the outbox creation timestamp. If found, the row is marked delivered. If not found or verification is itself unavailable, the row remains pending and retries later with exponential backoff; every retry performs the history check before attempting another send.

This is application-level deduplication because `welink-cli im send-to-group` does not currently expose a caller-supplied idempotency key/message ID.

Polling has its own transient backoff and does not block Task/Workflow execution.

## V1.0.1: serialize WeLink CLI operations

WorkBot may have several asynchronous activities that need WeLink at the same time: polling, an immediate reply, delivery verification after an ambiguous send, and durable-outbox retry.  These must not create concurrent `welink-cli` processes.  The adapter therefore owns one operation gate and runs exactly one complete WeLink operation at a time.

This is a WorkBot-process guarantee.  An independently launched manual `welink-cli` command does not participate in the gate.

Temporary tracing can be enabled with `scripts/configure-welink-trace.ps1`.  Trace commands redact message text and expose queue wait + subprocess duration so verification contention can be distinguished from slow CLI execution.
