# WorkBot Control Center GUI

V1.12.4 adds a Windows-friendly desktop control center built with Python's standard Tkinter UI toolkit. It is a **runtime GUI**, not only an installer frontend.

## Start

```powershell
.\scripts\run-gui.ps1
```

The script starts the Control Center and, by default, starts WorkBot inside the GUI process. To open the UI without starting WorkBot:

```powershell
.\scripts\run-gui.ps1 -NoAutostart
```

Do not start a second `run-workbot.ps1` instance for the same deployment when the GUI-hosted runtime is already active. V1.12.4's Control Center manages the WorkBot instance it starts; attaching to an independently started external WorkBot process is not part of this release.

## Runtime control

The top toolbar provides:

- Start WorkBot
- Stop WorkBot
- Restart WorkBot
- Run Doctor
- Manual refresh

The GUI does not maintain a second task state machine. It uses `WorkBotControlService` against the live WorkBot instance, while SQLite/TaskManager/WorkflowManager/NodeRegistry/RAG remain authoritative.

## Dashboard

The Dashboard shows:

- runtime state and uptime;
- WeLink receive mode / realtime connection state;
- CodeAgent scheduler active/waiting/stopping counts;
- active Task and Workflow counts;
- node online count;
- RAG coverage;
- durable outbox backlog;
- multi-Agent collaboration identity and peer count.

## Agent

The Agent page exposes both scheduler invocations and CodeAgent run diagnostics:

- active / waiting / stopping invocation id;
- purpose, conversation and elapsed time;
- `arun-*` status, PID, last-output age, error;
- bounded stdout/stderr tail;
- stop selected invocation;
- stop all CodeAgent invocations.

Stopping an Agent invocation uses the existing `AgentScheduler` cancellation path; it does not forge Task/Workflow receipts.

## Tasks and Workflows

Tasks can be inspected and operated through the existing manager APIs:

- cancel;
- retry;
- append instruction;
- steer current task turn.

Workflow controls include:

- cancel;
- resume a blocked workflow;
- retry a selected step.

All state changes still flow through the existing WorkBot control plane and remote worker protocol.

## Nodes and peer WorkBots

The page shows execution-node connectivity/load and peer WorkBots discovered by the group handshake protocol. **Discover WorkBots** manually sends one discovery hello to each configured collaboration group and shows the last discovery time. WorkBot does not broadcast hello at startup or on a timer.

Static peer pins remain supported, but `collaboration.peers` is optional when `discovery_enabled=true`.

## RAG

The RAG page displays the complete current `RAGService.status()` result and provides controls for:

- start/stop sync;
- start embedding a bounded number of chunks or all pending chunks;
- stop embedding.

Long-running RAG jobs remain background jobs inside `RAGService`; the GUI only starts/stops and observes them.

## Configuration

Two editors are provided:

1. **Common settings** for frequently changed values such as CodeAgent command/timeout/concurrency, aliases, self account, collaboration and RAG enablement.
2. **Raw JSON** for complete `config/local.json` editing.

Saving uses the existing setup service layer:

- config validation;
- backup rotation;
- atomic write;
- regenerated `AGENTS.md` from the allowlisted context.

Only `ingestion_policy`, `reply_policy`, and `execution_policy` are hot-reloaded by the running bot. Other parameters are explicitly restart-bound; use **Save & Restart** after changing them.

## Logs and Doctor

The Logs page displays a bounded live logging stream from the GUI-hosted WorkBot process. Doctor uses the existing fail-soft `workbot.setup.doctor` checks and renders every result in the desktop UI.

## Architecture

```text
Tk Control Center
      │
      ▼
RuntimeController (GUI thread ↔ asyncio thread)
      │
      ▼
WorkBotControlService
      │
      ├── WorkBot / AgentManager
      ├── TaskManager / WorkflowManager
      ├── NodeRegistry / SSHManager
      ├── RAGService
      └── SQLite authoritative state
```

This separation is intentional: a future Web UI can reuse `WorkBotControlService` without reimplementing WorkBot governance.
