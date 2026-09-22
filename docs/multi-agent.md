# WorkBot Peer-Agent Collaboration

WorkBot supports multiple independently deployed WorkBot instances in the same WeLink group. Peer discovery is manually triggered so idle bots do not fill the group with periodic protocol messages; static peer bindings remain available as optional pins.

## Design

Human-facing and machine-facing identity are separate:

1. **Human alias** — use names such as `WorkBot-A` and `WorkBot-B` so people can address one bot in a shared group.
2. **Machine identity** — peer traffic carries a structured `[WBOT]` envelope with `from`, `to`, `type`, `id`, `reply_to`, and `hop`.
3. **Transport identity** — the declared peer id is bound to the actual WeLink `sender_id` observed by WorkBot. The visible `[AGENT]` / `[AGENT-A]` prefix is never treated as authentication.

`im.self_accounts` is therefore required when collaboration is enabled: it lets an instance suppress its own outbound echo while accepting another WorkBot's `[AGENT]` messages.

## Manual discovery and handshake

Run `/agent discover` in an allowed collaboration group, or click **Discover WorkBots** in the Control Center. Each action sends exactly one `hello` to each selected group:

```text
[AGENT-A][WBOT]{"from":"workbot-a","hop":0,"id":"amsg-...","to":"*","type":"event","v":1}
{"agent_id":"workbot-a","aliases":["WorkBot-A"],"capabilities":["chat","codeagent","tasks","workflows","rag"],"display_name":"WorkBot-A","kind":"hello"}
```

A peer receiving the hello binds `workbot-a` to the **real WeLink sender account of that message** and returns one targeted `hello_ack`. An ack never triggers another hello. The binding is runtime state; it does not rewrite `config/local.json`.

At startup and while idle, WorkBot sends no discovery hello and starts no announcement timer. It only listens for valid `hello` / `hello_ack` events. `discovery_enabled=true` means the instance may participate in this manual handshake; it does not mean background broadcasting. The deprecated `auto_discovery` key is accepted as a compatibility alias with the same non-periodic meaning.

The binding uses a TOFU rule inside the explicitly enabled collaboration group:

- the first valid hello binds an `agent_id` to the observed sender account;
- messages from that binding refresh `last_seen`;
- another sender cannot take over the same live `agent_id` while the existing binding is fresh;
- after `peer_ttl_seconds`, a replacement handshake may establish a new runtime binding;
- an optional static peer with `sender_accounts` acts as a pin and overrides TOFU.

This removes the need to copy every colleague's WorkBot account into `collaboration.peers` while still using the transport sender identity as the trust anchor.

## Minimal configuration

For a dedicated WorkBot group, a typical instance only needs:

```json
{
  "im": {
    "self_accounts": ["YOUR_WELINK_ACCOUNT"],
    "groups": [
      {"group_id": "YOUR_AGENT_GROUP_ID", "group_name": "WorkBot Collaboration"}
    ],
    "intent": {
      "require_alias": true,
      "bot_aliases": ["WorkBot-A"]
    }
  },
  "collaboration": {
    "enabled": true,
    "agent_id": "",
    "groups": [],
    "discovery_enabled": true,
    "peer_ttl_seconds": 300,
    "accept_broadcast": false,
    "max_hops": 4,
    "peers": {}
  }
}
```

`agent_id=""` is allowed. WorkBot deterministically derives an id from the first `im.self_accounts` value. An empty `collaboration.groups` list reuses the configured `im.groups` group ids.

If you prefer a human-readable stable machine id, set one explicitly, for example `workbot-a`.

## Optional static pins

Static peers remain useful when a peer must be cryptographically/operationally pinned to a known enterprise account instead of TOFU:

```json
{
  "collaboration": {
    "enabled": true,
    "agent_id": "workbot-a",
    "discovery_enabled": true,
    "peers": {
      "workbot-b": {
        "sender_accounts": ["KNOWN_B_WELINK_ACCOUNT"],
        "aliases": ["WorkBot-B"],
        "description": "Database specialist"
      }
    }
  }
}
```

If a static peer declares `sender_accounts`, a discovery hello for that `agent_id` from a different sender is rejected.

## Request / response protocol

A request sent by `workbot-a` to `workbot-b` is rendered as:

```text
[AGENT-A][WBOT]{"from":"workbot-a","hop":0,"id":"amsg-...","to":"workbot-b","type":"request","v":1}
Please review the failing tests and suggest a patch.
```

A reply is emitted as:

```text
[AGENT-B][WBOT]{"from":"workbot-b","hop":1,"id":"amsg-...","reply_to":"amsg-...","to":"workbot-a","type":"response","v":1}
The failure is caused by ...
```

Only a verified, explicitly targeted `request` automatically wakes the destination Agent. `response` and ordinary `event` messages are durably stored as conversation context but do **not** automatically trigger another Agent turn. This is the primary Bot↔Bot ping-pong loop breaker.

Other protections remain:

- protocol ids are deduplicated;
- `max_hops` bounds delegated request chains;
- `to` must match the local `agent_id`, unless broadcast is explicitly enabled;
- collaboration identity and execution authorization are independent.

A peer can therefore be discovered and allowed to request read-only analysis without being granted `execution_policy` permission.

## Commands

```text
/agent peers
/agent discover
/agent send workbot-b Please inspect this issue and return your findings.
```

`/agent peers` shows static and discovered peers, including the current sender binding and last-seen age.

`/agent discover` is valid only in an allowed collaboration group and emits one hello in that group.

`/agent send` sends a structured request. A reasoning Agent may also propose:

```text
<WORKBOT_ACTION>{"type":"peer_request","peer":"workbot-b","instruction":"..."}</WORKBOT_ACTION>
```

That proposal still becomes a governed Pending Action; the current authorized human must confirm before WorkBot sends it.

## Pending Action group behavior

V1.12.4 deliberately treats natural confirmation words from the wrong sender as ordinary group chatter. If user A owns a Pending Action and user B says `yes`, `创建`, or `确认`, WorkBot:

- does not consume A's pending action;
- does not reply to B;
- does not disclose that a pending action exists.

This keeps natural-language convenience without turning common group words into noisy or privacy-leaking control messages.

## GUI

The WorkBot Control Center shows discovered peers under **Nodes & Peers** and provides **Discover WorkBots** for the same one-shot manual action. It also shows the peer count and last manual discovery time. See [GUI documentation](gui.md).
