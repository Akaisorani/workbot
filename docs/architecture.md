# WorkBot V0.7 architecture

## Responsibilities

### Windows WorkBot

- polls configured WeLink conversations and applies ACL before Agent execution;
- owns Conversation, PendingAction, Task, Workflow, Session metadata, summaries, and memory;
- invokes Windows CodeAgent for reasoning, local workflow steps, planning, summarization, memory curation, final synthesis, and read-only Linux-origin RPC queries;
- maintains reconnecting SSH relays to Linux workers;
- recovers persisted workflows after WorkBot restart without silently rerunning interrupted Windows side effects.

### IM concurrency model

The WeLink poller is an ingestion loop, not an execution loop. It persists a whole poll batch and dispatches work without waiting for CodeAgent.

- Ordinary messages are FIFO per Conversation because a CodeAgent conversation session is sequential state.
- Different Conversations have independent queues/workers and may use separate CodeAgent processes concurrently.
- Session-bound Agent calls are protected by a per-Conversation lock, including background Windows workflow steps, so the same session is never resumed simultaneously.
- Safe read-only/control commands such as `/status`, `/session`, memory lookup and explicit-ID cancellation have a fast lane and do not wait for a long conversational Agent turn.
- Tasks and workflows already created by WorkBot run as background jobs independently of IM polling.

This means one busy group still preserves dialogue order, while another group/private conversation and remote execution continue in parallel.

### Workflow

A Workflow is a DAG of generic CodeAgent steps. WorkBot stores executor, node, authoritative instruction, dependencies, state, attempts, result, and optional milestone flag. It does not maintain a closed task taxonomy.

Independent ready steps may run concurrently. Remote steps become child tasks. On restart:

- completed steps are reused;
- a still-running remote child task is tracked rather than recreated;
- pending steps continue normally;
- a Windows step that was `running` when WorkBot died becomes `interrupted` and blocks the workflow until the user explicitly resumes it.

### Conversation and CodeAgent session

The source of truth is the Conversation record, not the CodeAgent process. WorkBot assigns a UUID for a new logical session with `codeagent --session-id <uuid>` and resumes it with `--sessions <uuid>`. Sessions rotate after configurable turns/age. Recent messages + a persisted summary preserve continuity across rotation.

### Memory

Environment knowledge remains in `AGENTS.md/docs/skills`. Dynamic long-term memory is stored in SQLite with global or conversation scope. Retrieval uses FTS5 when available plus a LIKE fallback. Automatic memory capture is evidence-constrained and does not promote conversation summaries by default.

### Linux agent-worker

- runs independently of SSH as a daemon;
- accepts local Unix-socket clients and SSH relay clients;
- persists outbound events before transmission;
- launches generic CodeAgent tasks in task-specific tmux sessions;
- supports task cancellation;
- retransmits pending events after Windows reconnects;
- multiplexes synchronous Linux-originated RPC requests to Windows and routes responses back to the original local client.

### Linux -> Windows RPC

V0.7 adds read-only request/response over the existing SSH transport. It does **not** expose a Windows listening port and does not require Linux to SSH back to Windows.

Direct file methods are constrained by configured Windows read roots. `workbot.query` uses a fresh one-shot Windows CodeAgent, not the user's IM session, so it can run concurrently without contaminating conversation state. The RPC handler itself runs in a background task so a long office-side lookup cannot block remote task events on the SSH stream.

See `docs/rpc.md`.

## Lifetime separation

- Conversation lifetime: WorkBot SQLite.
- CodeAgent session lifetime: reusable/rotating session UUID.
- Workflow lifetime: WorkBot SQLite.
- Remote task lifetime: WorkBot + worker SQLite.
- Interactive process lifetime: tmux/psmux only where useful.
- Transport lifetime: reconnecting SSH relay.
- RPC lifetime: one local request/response, bounded by timeout and current upstream connection.

No terminal or Agent process is the source of truth.

## V1.11.0 local Hybrid RAG

The Windows supervisor owns a derived knowledge-retrieval layer in the same SQLite database. Source-of-truth business tables remain separate from retrieval indexes:

```text
memory_items / workspace_* / future source caches
                 |
                 v
     rag_documents -> rag_chunks
                   /            \
              rag_fts       versioned vec0
                   \            /
                    Hybrid RRF
                        |
                  Agent context
```

This layer is intentionally local and rebuildable. `sqlite-vec` is loaded only on vector operations, and Sentence Transformers models are lazy-loaded. Missing optional RAG dependencies must not prevent the supervisor, WeLink ingestion, approvals, workflows or legacy lexical retrieval from starting.

RAG access control is enforced by WorkBot before evidence reaches CodeAgent. In particular, conversation-scoped Memory is partitioned/filtered by the current conversation. CodeAgent may ask WorkBot for another read-only retrieval round through the internal `<WORKBOT_RAG>` protocol, but it must not directly query WorkBot's SQLite RAG tables.

The Linux worker should not maintain a second writable copy of the vector index. Windows remains the mutable RAG owner; remote work can consume retrieved evidence through the existing WorkBot orchestration/RPC context.
