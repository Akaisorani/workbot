# WorkBot

Current version: **V1.12.4**.


V1.12 adds rich-message context: quoted WeLink messages are preserved as explicit context, and local WeLink screenshots can be OCRed with optional RapidOCR support. Image/OCR failures degrade gracefully without blocking the text request.
[中文说明](README.md) · [Changelog](CHANGELOG.md) · [Architecture](docs/architecture.md) · [Security](docs/security.md) · [Hybrid RAG](docs/rag.md)

WorkBot is a Windows-centered, multi-node office-agent supervisor built around CodeAgent and WeLink. It combines durable conversation/session/task state, governed execution and approvals, remote Linux workers, workspace knowledge, long-term memory, and local Hybrid RAG (SQLite FTS5 + sqlite-vec + Sentence Transformers).



## V1.12.4 — Runtime Control Center GUI & Peer Handshake

- Added a full **WorkBot Control Center** desktop GUI. It hosts and controls the running WorkBot, with Start / Stop / Restart, dashboard metrics, CodeAgent runs/tails, Task/Workflow controls, node/peer state, RAG jobs, configuration editing, live logs, and Doctor diagnostics.
- The GUI uses the reusable `WorkBotControlService` over the existing authoritative managers/SQLite state; it does not create a parallel task state machine.
- Natural pending words from a non-authorized group member are now silently ignored as control input. WorkBot no longer reveals that somebody else currently owns a Pending Action.
- Peer WorkBots are discovered through a one-shot `hello / hello_ack` handshake triggered by `/agent discover` or **Discover WorkBots** in the GUI. Startup and idle operation no longer broadcast periodic hello messages; `discovery_enabled=true` allows manual handshake participation.
- `collaboration.agent_id` may be blank and is deterministically derived from `im.self_accounts`; an empty collaboration group list reuses configured WeLink groups.

Recommended Windows runtime entry:

```powershell
.\scripts\run-gui.ps1
.\scripts\run-gui.ps1 -NoAutostart
```

See [WorkBot Control Center GUI](docs/gui.md) and [Peer-Agent collaboration](docs/multi-agent.md).


## V1.12.3 — Policy Hot Reload & Peer-Agent Collaboration

V1.12.3 adds atomic hot reload for `ingestion_policy`, `reply_policy`, and `execution_policy`; updates the default CodeAgent arguments to `--skip-safe-check --permission-mode bypassPermissions -p {prompt}`; removes the enabled `linux-server1` sample node from the runtime example config; and adds identity-bound WorkBot-to-WorkBot collaboration in shared groups.

Peer identity is verified against the actual WeLink sender account, not the visible `[AGENT]` prefix. Only an explicitly addressed verified `request` wakes the target WorkBot. `response` and `event` messages are stored as conversation context but do not automatically trigger another turn, which prevents bot-to-bot ping-pong loops. Operators can use `/agent discover`, `/agent peers`, and `/agent send <peer-id> <request>`. See [Peer-Agent collaboration](docs/multi-agent.md).

## V1.12.2 — Usability & Observability

V1.12.2 adds ownership-bound natural Pending Action confirmation, config-driven `AGENTS.md` generation, live CodeAgent run observability (`/agent status`, `/agent tail [N]`), and reusable setup/configuration/doctor services with PowerShell wrappers. CodeAgent stdout/stderr remains diagnostic observation only; authoritative Task/Workflow state continues to come from WorkBot receipts.

New installation entry point:

```powershell
.\scripts\install.ps1
.\scripts\install.ps1 -Quick
.\scripts\doctor.ps1
```

## Highlights

- **WeLink-native assistant**: group/private-chat ingestion, realtime Hook support, strict alias dispatch, durable outbound retry, native code blocks/tables, long-message splitting, and direct file attachments.
- **Governed execution**: independent ingestion/reply/execution/action policies, explicit approvals for side effects, idempotent approval continuation, and managed Linux task isolation.
- **Multi-node orchestration**: Windows supervisor + persistent SSH Linux workers, deterministic node scope, workflow DAGs, task steering, cancellation, and read-only Linux→Windows RPC.
- **Persistent knowledge**: Conversation summaries, long-term Memory, local/remote Workspace Knowledge, evidence-grounded answers, and GaussDB-aware research defaults.
- **Hybrid RAG**: canonical RAG corpus, FTS5 lexical retrieval, Qwen3 embeddings, sqlite-vec cosine search, weighted RRF, optional reranking, passive Agent context, and controlled active RAG requests.
- **Fail-soft design**: optional ML/vector dependencies do not prevent WorkBot from starting; lexical retrieval remains available when embeddings/sqlite-vec are unavailable.

## Requirements

- Windows control host with Python 3.11+
- `codeagent` available in PowerShell
- `welink-cli` and the required enterprise WeLink environment
- OpenSSH client for remote Linux workers
- Optional for RAG: `sqlite-vec`, `sentence-transformers`, and local Qwen3 model cache

## Quick start

```powershell
cd D:\code\workbot
.\scripts\setup-windows.ps1
```

Edit the generated `config\local.json`, then run:

```powershell
.\scripts\run-workbot.ps1
```

For local Hybrid RAG:

```powershell
.\scripts\setup-vision.ps1
```

The optional vision setup installs RapidOCR + ONNX Runtime for local screenshot OCR.

```powershell
.\scripts\setup-rag.ps1 -DownloadModel
```

Then from WeLink:

```text
/rag sync
/rag embed all
/rag status
```

Long RAG sync/embed operations run as observable background jobs. On CPU-only machines, embedding a large source tree can take many hours; WorkBot commits micro-batches incrementally and can continue serving interactive traffic.

## Release validation

Run the single consolidated release check:

```powershell
.\scripts\check-release.ps1
```

Optional local-runtime probes:

```powershell
.\scripts\check-release.ps1 -CodeAgent
.\scripts\check-release.ps1 -RagRuntime
```

The full `tests/` suite is the authoritative regression set. Historical version-specific `check-v*.ps1` scripts were removed from the release tree.

## Main operator commands

```text
/status
/nodes
/sessions
/agent status
/agent tail [N]
/agent discover
/agent peers
/agent send <peer-id> <request>
/stop-session <agent-id>
/stop-sessions all
/memory [query]
/workspace [query]
/rag status
/rag sync [status|stop]
/rag embed [N|all|status|stop]
/rag search <query>
/rag reindex
/approve <approval-id>
/deny <approval-id> [reason]
/steer <task-id> <instruction>
/add <task-id> <instruction>
```

Valid WorkBot slash commands may be used directly even when strict alias mode is enabled. Ordinary chat requires the configured bot alias at the beginning of the human-readable text (recognized media may precede it).

## Configuration

The tracked template is `config/workbot.example.json`. Runtime configuration belongs in `config/local.json`, which is intentionally ignored by Git.

Important sections include:

- `im`: WeLink transport, discovery, realtime Hook, alias dispatch
- `agent`: CodeAgent command, sessions, concurrency, prompt args, optional GaussDB context
- `access_control` / `reply_policy` / `execution_policy` / `action_policy`: access and side-effect governance
- `nodes`: remote Linux worker topology and capabilities
- `collaboration`: local agent identity, manual discovery/static peer bindings, and collaboration groups
- `memory`: capture and cross-conversation retrieval behavior
- `workspace_knowledge`: local/remote indexed source roots and scan bounds
- `rag`: embedding/vector/index/retrieval/reranker configuration
- `rpc`: allow-listed Linux→Windows read-only RPC

V1.12.3 uses an allow-listed hot-reload model. `ingestion_policy`, `reply_policy`, and `execution_policy` are re-read from `config/local.json` (2-second default interval) and replaced atomically only after all three validate. Invalid edits leave the last known-good policies active. Other configuration, including `action_policy`, `agent.prompt_args`, model selection, aliases/self identity, node topology, collaboration peer topology, RAG runtime settings, and WeLink transport settings, still requires a WorkBot restart.

## Repository layout

```text
workbot/                 Python control plane
linux/                   Linux worker/runtime payload
skills/                  Agent procedures and governed tool guidance
config/                  tracked example config only
docs/                    architecture, policy, RAG and operations docs
nodes/                   node documentation examples
scripts/                 setup/configuration/runtime/release utilities
tests/                   full regression suite
AGENTS.example.md         template for generated local AGENTS.md
CHANGELOG.md              consolidated release history
README.md              Chinese documentation
```

Runtime state, databases, logs, local configuration and local model caches are excluded by `.gitignore`.

## Documentation

- [Chinese README](README.md)
- [Architecture](docs/architecture.md)
- [WorkBot Control Center GUI](docs/gui.md)
- [Peer-Agent collaboration](docs/multi-agent.md)
- [Access control](docs/access-control.md)
- [Action policy](docs/action-policy.md)
- [Memory](docs/memory.md)
- [Hybrid RAG](docs/rag.md)
- [WeLink tools](docs/welink-tools.md)
- [IM reliability](docs/im-reliability.md)
- [Remote workspace knowledge](docs/remote-workspace.md)
- [Workflows](docs/workflows.md)
- [Security](docs/security.md)
- [Release guide](docs/release-guide.md)
- [Changelog](CHANGELOG.md)

## Version

Current release: **1.12.4**.
