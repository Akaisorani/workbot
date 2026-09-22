# Realtime WeLink transport (V1.5.2)

V1.5 uses WeLinkBot's local WebSocket Hook as the primary **receive-only** transport. `welink-cli` remains authoritative for sending messages and for mail/calendar/meeting/OneBox/tool actions through WorkBot's managed gateway.

## Why push-first

The OAuth history API is polling-only and `query-history-message` has a documented 20 calls/minute ceiling. V1.5 therefore separates:

```text
normal receive        WeLinkBot Hook -> ws://127.0.0.1:4080 -> WorkBot
send/business tools   WorkBot -> managed welink-cli gateway -> WeLink
recovery/backfill     WorkBot -> welink-cli query-history-message
```

When the WebSocket is healthy, CLI history polling is standby/reconciliation rather than the normal receive path. If the WebSocket disconnects, WorkBot immediately falls back to the V1.4 polling path. On first connect or reconnect it opens a bounded backfill window so messages missed while WorkBot/WeLinkBot was down can be recovered.

## Configuration

Run:

```powershell
edit `config/local.json -> im.realtime` (start from `config/workbot.example.json`)
```

Default settings:

```json
"realtime": {
  "enabled": true,
  "url": "ws://127.0.0.1:4080",
  "secret": "",
  "secret_env": "WORKBOT_WELINKBOT_SECRET",
  "fallback_poll_interval_seconds": 3,
  "reconcile_interval_seconds": 300,
  "backfill_after_connect_seconds": 120,
  "reconnect_initial_seconds": 1,
  "reconnect_max_seconds": 30,
  "queue_max_messages": 5000
}
```

The WeLinkBot executable is an external dependency and is **not** distributed in the WorkBot archive. Start it separately before WorkBot, or leave it unavailable: WorkBot will remain functional through CLI polling fallback.

Prefer a secret supplied by `WORKBOT_WELINKBOT_SECRET`. An empty secret on a loopback-only endpoint is permitted for local testing. WorkBot logs a high-risk warning for a non-loopback WebSocket endpoint with no secret.

## Message normalization

Only frames shaped as:

```json
{"type":"weLinkMessage","func":"receiveIMMessage","data":{...}}
```

enter the IM pipeline.

Observed Hook semantics are normalized as follows:

- group: `chatType=1` and non-empty `groupId` -> `welink:group:<groupId>`;
- direct message: `chatType=0`; if `isMine=true`, peer is `receiverAccount`/`recentOwner`, otherwise peer is `userAccount`;
- `isPrivateChat` is not used as the primary DM discriminator because real Hook samples can report `0` for direct messages;
- text priority: `showText` -> `solidContent` -> nested XML/CDATA `content`;
- message identity: `msgId` -> `clientMsgId` -> local `id` -> deterministic hash;
- `isAt` is preserved as a high-confidence directed-to-Bot signal.

A manual outgoing DM (`isMine=true`) is stored as SELF context but never activates the Agent. Group messages typed by the operator are not classified as SELF merely because `userAccount` equals the WorkBot account, so commands typed in a Bot group continue to work.

## Loop prevention

WorkBot messages sent by `welink-cli` are also visible to the Hook. Realtime normalization suppresses those echoes using the existing `[AGENT]` prefix and short-lived outbound fingerprint cache. This happens before Conversation dispatch, preventing self-reply loops.

## Cursor and dedup behavior

Push and history converge on the same `IncomingMessage`/Conversation Store path, but **provider message ID alone is not authoritative**. Hook traffic can expose `clientMsgId`/local `id`/no ID while history later exposes a numeric `msgId` for the same message. V1.5.2 uses:

1. exact `(conversation_id, external_message_id)` matching;
2. a transport-independent logical fingerprint from conversation, sender, `serverSendTime` and normalized text;
3. a narrow ±2.5s sender/content match only across different transports (or legacy rows) to tolerate small Hook/API timestamp skew.

The third rule is deliberately cross-transport only: two intentional identical messages sent on the same transport remain distinct. Duplicate history aliases still advance fallback cursors so the same recovered message is not reconsidered on every sweep.

After a pushed message is successfully stored, WorkBot advances fallback cursors:

- numeric `msgId` when available;
- `serverSendTime` as the fallback cursor for Hook DMs without a numeric history ID.

Cursors advance **after durable ingestion**, not when a WebSocket frame is merely received. If the realtime queue ever overflows, dropped data therefore remains eligible for CLI history recovery.

## Health and recovery

`/welink` reports:

- receive transport and mode (`realtime`, `backfill`, `fallback`);
- WebSocket connected/authenticated state;
- connection/reconnect counts;
- realtime message/queue/drop counts;
- last event/message age;
- next CLI reconciliation and backfill remaining time;
- normal CLI/history budget diagnostics.

Reconnect uses exponential backoff (1s -> 2s -> 4s ... capped at 30s by default). Each successful auth increments a backfill generation and asks the CLI adapter to reconcile known conversations.


## Approval replay safety

Realtime/history duplicates can include fast-lane control messages such as `/approve approval-...`. Message dedup is the first guard, but V1.5.2 also makes approval execution independently idempotent. `ApprovalManager.decide()` reports whether a durable state transition actually occurred; only the first `pending -> approved` transition is allowed to schedule a Conversation Agent resume. The resume then atomically claims a persistent `resume_state`, so even a second code path cannot perform the approved write twice.
