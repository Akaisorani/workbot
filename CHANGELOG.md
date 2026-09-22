## Unreleased

- Replaced periodic peer `hello` announcements with explicit one-shot discovery from `/agent discover` or the GUI's **Discover WorkBots** action.
- Added `collaboration.discovery_enabled`; legacy `auto_discovery` remains a non-periodic compatibility alias, and `announce_interval_seconds` is obsolete.
- Added peer discovery count/last-run status to the GUI and runtime control snapshot.

---

## 1.12.4 — Runtime Control Center GUI & Peer Handshake

- Added the Tkinter-based `WorkBot Control Center` runtime GUI with Start/Stop/Restart, dashboard, Agent run/tail inspection, Task/Workflow controls, node/peer views, RAG job controls, configuration editing, live logs, and Doctor integration.
- Added reusable `WorkBotControlService` and thread-safe `RuntimeController` so GUI operations reuse WorkBot's existing authoritative managers and can be reused by a future Web UI.
- Changed non-owner natural Pending Action replies (`yes`, `创建`, etc.) to silent control-plane ignore: the pending action remains untouched and its existence is not disclosed to unrelated group participants.
- Added peer auto-discovery through structured `hello` / `hello_ack` events. Runtime identity is bound to the actual WeLink sender account; active bindings reject conflicting sender takeovers until the peer TTL expires.
- Made `collaboration.peers` optional in auto-discovery mode, allowed a blank `agent_id` to derive from `im.self_accounts`, and let an empty collaboration group list reuse configured WeLink groups.
- Added `scripts/run-gui.ps1`, `workbot-gui` GUI entry point, `docs/gui.md`, and V1.12.4 regression coverage.

---

# Changelog

## 1.12.3 — Policy Hot Reload & Peer-Agent Collaboration

- Added atomic runtime hot reload for `ingestion_policy`, `reply_policy`, and `execution_policy`; invalid edits keep the last known-good policy set.
- Updated default/example CodeAgent `prompt_args` to `--skip-safe-check --permission-mode bypassPermissions -p {prompt}`.
- Removed the enabled `linux-server1` node from `config/workbot.example.json`; example installs now start with `nodes={}` and `default_node=null`.
- Added identity-bound peer WorkBot collaboration in shared WeLink groups using a structured `[WBOT]` envelope (`from/to/type/id/reply_to/hop`).
- Changed WeLink self-echo suppression so `[AGENT]` messages from other configured sender accounts are ingested instead of being mistaken for this instance's own output; legacy deployments without `self_accounts` retain prefix-only suppression for loop safety.
- Added `/agent peers` and `/agent send <peer> <request>`, plus governed model-proposed `peer_request` Pending Actions.
- Added loop controls: only targeted verified `request` messages auto-dispatch; `response/event` messages are store-only, protocol ids are deduplicated, and request chains are bounded by `max_hops`.

## 1.12.2 — Usability & Observability

- Added ownership-bound natural Pending Action confirmation/cancellation before strict alias gating.
- Added `AGENTS.example.md` plus allowlisted config-driven `AGENTS.md` generation with legacy manual-file backup behavior.
- Added streaming CodeAgent stdout/stderr observation with `arun-*`, bounded tails, timeout diagnostics, `/agent status`, and `/agent tail`.
- Added reusable Python setup services (`configurator`, `validator`, `agents_generator`, `doctor`, `wizard`) plus install/configure/doctor/generate PowerShell entry points.
- Preserved receipt-based authoritative Task/Workflow state, existing DB compatibility, and existing RAG indexes.

## V1.12.1

- Added mobile WeLink quote/reply card normalization. `chatContentType=10` / `cardType=65` payloads whose JSON is embedded in XML CDATA are normalized before alias gating: `cardContext.replyMsg.content` becomes the current message and `cardContext.preMsg` becomes the quote context.
- Desktop and mobile quote payloads now share the same downstream semantics for alias matching, read-only Q&A, delegated execution, provenance, and conversation persistence.
- Local images are exposed to CodeAgent as trusted local file attachments for direct multimodal inspection. OCR is now explicitly supplementary rather than the sole image-understanding path.
- If OCR is unavailable or fails but the trusted image file exists, WorkBot preserves the image as `available` multimodal context instead of degrading it to an unusable attachment. Text processing remains fail-open when image discovery or analysis fails.

---

## V1.12.0

- Added WeLink quote/reply parsing. Normalized quoted sender, message ID, text and referenced image context are persisted with the conversation message.
- Added quote-aware delegated execution: commands such as `处理这个` can reuse a quoted executable request while preserving `requested_by` / `authorized_by` provenance and the original message ID.
- Added local WeLink image-path extraction from `fileList`, `showHtml` and UM image markers. Both self-sent and peer-sent screenshot payload shapes are supported.
- Added optional RapidOCR + ONNX Runtime image text extraction through the `vision` optional dependency and `scripts/setup-vision.ps1`.
- Image lookup/OCR is fail-open for text processing: missing files, disabled dependencies, OCR timeouts and OCR errors do not discard the remaining message.
- Added `messages.context_json` with automatic in-place SQLite migration for quote/image/OCR metadata.
- Restricted OCR reads to derived WeLink ReceiveFiles roots or explicitly configured `allowed_roots`; quote/OCR content is treated as untrusted context rather than instructions.

---

## V1.11.4

- Standardize README names: Chinese documentation is `README.md`; English documentation is `README_EN.md`.
- Update release hygiene checks to require `README.md` and `README_EN.md`.
- Fix `scripts/setup-rag.ps1` on Windows PowerShell: Python probes no longer receive here-strings through native stdin, avoiding injected `U+FEFF`/BOM characters.
- Make RAG setup fail fast when pip, sqlite-vec probe, or embedding model loading returns a non-zero exit code; `WORKBOT_RAG_SETUP_OK` is printed only after all requested checks succeed.

All notable WorkBot changes are consolidated here. Historical per-version `UPDATE_V*.md` files were merged into this document for the release tree.

## 1.11.3 - Execution receipt guard and delegated task recovery

- Added a program-level guard for model-authored claims about WorkBot-managed remote tasks/workflows. A remote execution claim is authoritative only when it carries a real durable `task-...` / `wf-...` receipt; otherwise the text is withheld.
- When an ungrounded execution-state claim is detected, WorkBot re-enters the same CodeAgent session with tools disabled and asks the Agent to re-examine the original request. The repair turn must either emit the correct structured `WORKBOT_ACTION` proposal or explicitly state that execution has not started. Repeated false claims are replaced by a safe WorkBot-authored message.
- Structured action preambles are sanitized so an Agent cannot say “already launched” in prose while simultaneously only proposing a task/workflow. WorkBot remains the sole authority for created/running state.
- Added deterministic delegated execution for operator messages such as `处理上面 p300... 的任务`: WorkBot resolves the referenced sender's recent executable request from durable conversation history and reroutes the original instruction under the current operator's permission.
- Added task/workflow provenance fields `requested_by`, `authorized_by`, and `authorization_message_id`. The original request message remains the execution origin while the later operator message records authorization.

---

## 1.11.2 - Release cleanup

- Added `README.md` and rewrote the main README around the current architecture and installation flow.
- Expanded `.gitignore` for runtime state, SQLite WAL/SHM files, logs, local config/secrets, caches, build artifacts and local model caches.
- Consolidated historical version check scripts into `scripts/check-release.ps1`; the full pytest suite remains the authoritative regression set.
- Removed version-specific migration/check/test shell scripts that were only useful during incremental development; current generic setup/configuration/runtime diagnostics remain.
- Consolidated all historical update notes into this file and removed root-level `UPDATE_V*.md` clutter.
- Sanitized example WeLink group identifiers and refreshed release/build documentation.

---

## 1.11.1

V1.11.1 is a runtime/operability update to the V1.11.0 Local Hybrid RAG release. It addresses issues discovered on the real Windows CPU deployment with ~75k documents / ~175k chunks and fixes operator execution identity in private WeLink conversations.

## 1. CPU embedding: background jobs instead of multi-hour blocking commands

A production `/rag embed 5000` on CPU can take hours. V1.11.0 performed the whole requested amount inside one command handler, so the originating conversation saw no progress until all chunks completed.

V1.11.1 changes the operator surface:

```text
/rag embed 5000
```

now starts a background embedding job and returns immediately. The job repeatedly processes a bounded micro-batch (`rag.index.job_batch_chunks`, default 32), while Sentence Transformers keeps its own model batch (`rag.embedding.batch_size`, default 16).

New controls:

```text
/rag embed all
/rag embed status
/rag embed stop
```

Status reports:

- chunks embedded by the current job;
- corpus embedded/pending counts and coverage percent;
- measured average chunks/sec;
- elapsed time and ETA;
- vector backend/index and last error.

Stop is cooperative. It cannot interrupt a PyTorch/SentenceTransformer call in the middle of the current micro-batch, but the job exits before starting the next one.

WorkBot deliberately does not launch multiple concurrent embedding model workers. When CPU usage is already saturated, more workers normally create contention rather than useful throughput. `rag.embedding.cpu_threads` is available for operators who prefer to reserve CPU headroom for interactive WorkBot traffic; `0` keeps PyTorch automatic thread selection.

Pending chunks are now prioritized roughly as:

```text
memory -> product manual -> wiki -> web -> code -> generic workspace
```

so a partially embedded corpus gains high-value semantic retrieval sooner.

## 2. Sync becomes observable/background and no longer overlaps RAG maintenance

The first `/rag sync` may take minutes for a large workspace. It now starts a background sync job and returns immediately.

New controls:

```text
/rag sync status
/rag sync stop
```

Progress separates:

- documents processed;
- documents whose canonical RAG record changed;
- chunks upserted;
- current phase (`memory` / `workspace`);
- elapsed time and error state.

A manual sync/embed job suppresses idle RAG maintenance, and RAG mutation paths share an index guard. This prevents a user-triggered multi-hour embed from racing a maintenance sync/embed pass.

## 3. Cross-conversation Memory RAG by default

V1.11.0 restricted Memory RAG to:

```text
global + conversation:<current conversation>
```

This is too strict for projects spread across multiple WeLink groups/DMs. V1.11.1 defaults to cross-conversation Memory retrieval. The original scope remains stored and appears in source provenance, but it no longer prevents a relevant memory from another conversation from being recalled.

Compatibility mode is still available:

```json
{
  "memory": {
    "cross_conversation_retrieval": false
  },
  "rag": {
    "retrieval": {
      "memory_scope": "conversation"
    }
  }
}
```

The first option keeps the legacy lexical fallback consistent; the second applies to Hybrid RAG / sqlite-vec.

## 4. Private-chat execution uses the same operator allowlist as groups

`PolicyEngine` itself already applied `execution_policy.allow_senders` independent of group/private conversation type. The weak point was WeLink DM normalization: for `isMine=true` manual outgoing DMs, Hook/history payloads can expose `userAccount`/`sender` inconsistently, so the sender ID reaching Execution Policy could differ from the operator account that works in groups.

V1.11.1 fixes both realtime and history normalization:

- `isMine=true` is authoritative for a manual local DM;
- if exactly one `im.self_accounts` entry exists, it becomes the normalized sender identity;
- if `im.self_accounts` is omitted and `execution_policy.allow_senders` contains exactly one account, WorkBot infers that account as the local self identity;
- a policy-side fallback grants this only to `conversation_kind=user AND from_self=true`; inbound peer DMs never receive it.

Thus a single-operator deployment can use:

```json
"execution_policy": {
  "default": "deny",
  "allow_senders": ["owner-account"]
}
```

for both group and private execution/approval flows.

## 5. Query/document embedding observability

Qwen document embeddings remain prompt-free; only queries use the `query` prompt. WorkBot now emits explicit logs such as:

```text
RAG embedding documents start ... mode=document prompt=none ...
RAG embedding query start ... mode=query prompt=query ...
```

whereas Sentence Transformers' own `Loaded 1 prompt ... ['query']` log only means the model exposes that prompt.

## 6. Status additions

`/rag status` now includes:

- embedded/pending chunks;
- embedding coverage percent;
- configured embedding batch/device/CPU thread setting;
- Memory retrieval mode (`all` or `conversation`);
- current manual sync/embed job states.

## Validation

Focused tests cover:

- default cross-conversation Memory retrieval;
- opt-in V1.11.0 Memory isolation compatibility;
- background numeric embedding jobs split into micro-batches;
- cooperative embedding stop;
- background sync progress;
- realtime private `isMine=true` sender normalization;
- execution allowlist inference for private manual messages;
- private operator dispatch with the same allowlisted account used in groups.

Run:

```powershell
cd D:\code\workbot
.\scripts\check-v1111.ps1
```


---

## 1.11.0

## Goal

Upgrade the existing Memory + Workspace knowledge system from lexical-only lookup to a local evidence-oriented Hybrid RAG layer without adding a separate vector database or making heavy ML dependencies mandatory for WorkBot startup.

## Added

### Canonical RAG layer

New `workbot/rag/` package:

- `embedding.py`: lazy Sentence Transformers provider plus deterministic test provider.
- `chunking.py`: source-aware semantic chunking.
- `store.py`: canonical RAG schema, FTS5, versioned vector indexes, sqlite-vec/Python vector persistence.
- `service.py`: incremental sync/embedding, hybrid retrieval, RRF, source-aware context and future-source API.
- `reranker.py`: optional lazy CrossEncoder reranking.

The canonical layer is derived from existing business data; no embedding columns were added to `memory_items` or `workspace_*`.

### Default embedding stack

- model: `Qwen/Qwen3-Embedding-0.6B`
- dimension: 512
- Sentence Transformers provider
- query prompt name: `query`
- normalized vectors
- cosine vector distance
- `sqlite-vec==0.1.9` as the pinned optional vector extension

The model and sqlite extension are optional/lazy. WorkBot continues to run with lexical RAG when they are unavailable.

### Hybrid retrieval

- FTS5 lexical top-k
- dense-vector top-k
- weighted Reciprocal Rank Fusion (RRF)
- exact identifier/path/section/symbol boost
- per-document diversity
- optional Qwen3 reranker (disabled by default)

### Memory + Workspace integration

- one Memory item -> one scoped RAG document
- `global` + current `conversation:<id>` isolation retained
- Workspace documents and source code sync into the canonical RAG layer
- code chunks preserve source path, symbol and line references where heuristically available
- `/memory <query>` and `/workspace <query>` prefer the new Hybrid RAG path and retain legacy fallback
- `/remember`, `/forget`, automatic memory extraction and promotion keep the RAG text layer synchronized

### Agent integration

- passive RAG injects relevant evidence before a normal Agent turn
- active `<WORKBOT_RAG>{...}</WORKBOT_RAG>` requests allow up to two focused retrieval rounds
- retrieval is executed by WorkBot, not direct Agent SQL
- code retrieval is explicitly candidate localization; precise implementation conclusions should still inspect the real source
- source markers flow naturally into the existing evidence-grounded response policy

### Operator controls

- `/rag status`
- `/rag sync`
- `/rag embed [N]`
- `/rag search <query>`
- `/rag reindex`

### Incremental/versioned indexing

- chunk content hashes prevent unnecessary re-embedding
- embedding model/revision/dimension identify a versioned vector index
- old vector indexes can coexist during model migration
- changing chunker version invalidates affected derived chunks
- an existing Python fallback vector index can be promoted to sqlite-vec when the optional dependency becomes available

### Future-source API

`RAGService.upsert_text_document()` allows future product-manual, Wiki, Web/W3, connector and other cached documents to reuse the same chunk/vector/retrieval infrastructure.

## Optional dependency setup

```powershell
cd D:\code\workbot
.\scripts\setup-rag.ps1 -DownloadModel
```

The `rag` optional extra installs:

- `sqlite-vec==0.1.9`
- `sentence-transformers>=5.4,<6`
- `transformers>=4.51,<5`

## First indexing

```text
/rag sync
/rag embed 100
/rag status
```

Embedding can continue via repeated bounded batches or normal idle maintenance.

## Configuration

See `config/workbot.example.json` and `docs/rag.md`. The optional reranker is disabled by default to avoid loading a second ~0.6B model for every deployment.

## Validation

Build-environment regression command:

```powershell
.\scripts\check-v1110.ps1
```

The release tests cover canonical sync, incremental embedding, FTS5 + vector RRF, conversation-memory isolation, code symbol chunking/source markers, future Wiki/manual/Web sources, active Agent RAG, optional reranking, and graceful operation without optional ML dependencies.


---

## 1.10.4

## 1. WeLink native code-block spacing

V1.10.3 kept the newline after a Markdown closing fence as a text segment **and** re-added another newline while rebuilding Markdown before native rendering. This made code blocks appear to have extra blank rows below them.

V1.10.4 fixes the parser/normalizer contract:

- the parser leaves original post-fence whitespace in the source stream;
- the normalizer no longer synthesizes another newline after the closing fence;
- native rendering compacts repeated blank lines immediately after a code block to one normal line break for chat readability;
- code-body whitespace and ordinary prose paragraph spacing remain unchanged.

Example logical Markdown:

```text
```python
D:\work\a.docx
```


正文
```

The WeLink wire text now has only one line break between the native-code metadata and `正文`.

## 2. Direct WeLink file attachments

WorkBot now treats direct attachment sending as a first-class WeLink capability:

```text
welink-cli im send-to-group --group-id "<group-id>" --file "<absolute-path>"
welink-cli im send-to-user --receiver "<account>" --file "<absolute-path>"
```

These commands are classified as one `im.send` write operation. The CLI may internally upload/create a share object while sending the attachment, but the Agent must not separately request `cloud.write`/`cloud.share` merely to send the file.

For the current conversation, WorkBot supplies the exact group-id/receiver in the Agent prompt. If the user explicitly asks to send/attach a local file:

1. verify/read the path as needed;
2. request one `im.send` approval;
3. after approval, invoke the direct `--file` command;
4. use OneBox/share-link only if the user explicitly asks for it or the direct attachment path actually fails.

Ordinary text replies still belong to WorkBot's durable outbound queue and should not be sent by CodeAgent through `welink-cli`.

## Validation

Run:

```powershell
.\scripts\check-v1104.ps1
```


---

## 1.10.3

## 1. Evidence-grounded user-visible answers

WorkBot now explicitly requires source attribution for material analytical conclusions whenever CodeAgent actually relies on retrieved evidence. Provenance should appear close to the supported conclusion instead of being detached from the reasoning.

Recommended forms include:

```text
[源码: path/to/file.cpp::Symbol]
[产品手册: 《document title》 -> chapter/section]
[Wiki: page title | URL]
[W3/Web: page title | URL]
[文件: path/to/file -> section]
```

If exact source line numbers are unavailable, CodeAgent must cite the file plus the verified symbol/function/section rather than inventing line numbers. It must not fabricate file names, manual chapters, Wiki page titles, URLs, or web results. Conversation history, Memory and `AGENTS.md` remain context rather than authoritative product evidence.

Workflow synthesis now preserves source markers from child-step results so provenance is not lost in the final answer. Linux-node guidance also asks delegated Agents to retain source identifiers returned through Windows read-only RPC.

## 2. GaussDB-aware database research defaults

For implementation-specific database questions where the user does not explicitly name another database, WorkBot now treats GaussDB as the likely background when that assumption materially affects the answer. Clearly database-independent/SQL-standard questions are not forced into GaussDB-specific framing.

Default Windows evidence sources are:

```text
GaussDB source:   D:\workbot\workspace\source
Product manuals:  D:\workbot\workspace\manuals
Wiki MCP entry:   https://wiki.example.com/workbot
```

The source strategy in the Agent prompt is:

- current implementation / optimizer / executor / Stream / kernel flow -> inspect source code when useful;
- documented behavior, configuration, specifications and supported contract -> prefer/cross-check product manuals;
- internal design rationale/background -> `wiki-mcp` using the configured Wiki entry URL;
- external/public background -> W3/web search.

If sources disagree, the answer should expose the disagreement instead of silently choosing one.

These defaults are available without changing an existing `config/local.json`. They may be overridden under `agent.gaussdb_context` in configuration.

## 3. `[AGENT]` is the new default reply prefix

The default `bot_reply_prefix` is now:

```text
[AGENT]
```

The transport still recognizes legacy `[自动回复]` history messages as old WorkBot echoes during rolling upgrades/reconciliation, preventing them from re-entering Agent dispatch. New outbound replies use the currently configured prefix.

## Validation

Run on Windows:

```powershell
.\scripts\check-v1103.ps1
```

The script runs focused provenance/GaussDB/prefix tests, the previous V1.9-V1.10 delivery/formatting regressions, then the complete test suite.

Build regression result: `215 passed`.


---

## 1.10.2

## 1. Durable reply ownership / meta-only Agent recovery

WorkBot now makes the current-conversation delivery contract explicit: CodeAgent computes the substantive answer; WorkBot's durable outbound queue owns delivery to WeLink.

The conversation Agent prompt now states that phrases such as “告诉我 / 回复我 / 然后告诉我” mean “return the actual answer text to WorkBot”, not “use WeLink to send it yourself”. The Agent must not replace a substantive result with meta-commentary such as `Already processed and summary delivered to the user.`

A second guard detects short delivery-meta-only final text. When this happens WorkBot resumes the same CodeAgent session with WeLink/tools disabled and asks only for the already-computed substantive result. It does not repeat the history query, summarization, or side effect. If the text cannot be recovered, WorkBot reports that honestly instead of claiming delivery succeeded.

The existing durable outbox behavior is retained and regression-tested: if the WeLink send attempt is ambiguous/transient, the exact persisted reply text remains pending and is retried directly from SQLite. The retry path does not invoke CodeAgent again.

## 2. Known slash commands may bypass `require_alias`

`im.intent.require_alias=true` continues to require a bot alias for ordinary natural-language requests, but valid WorkBot slash commands can again be typed directly, matching normal command-line/chatbot usage.

Examples that bypass the alias gate:

```text
/status
/sessions
/memory stream pbe
/workspace stream_pbe_sender
/summary refresh
/stop-sessions all
/retry
```

The bypass is not based on the first character alone. WorkBot matches a whitelist of supported command forms plus basic argument syntax. Therefore these are store-only and do not invoke WorkBot:

```text
/foo
/status unexpected-argument
/:um_begin{https://...|Img|...}/:um_end
```

Unknown slash-looking text is rejected even when `require_alias=false`, providing a second transport-safety layer if a future WeLink media/protocol string escapes normalization.

## Validation

Build regression suite:

```text
211 passed
```

Run on Windows:

```powershell
.\scripts\check-v1102.ps1
```


---

## 1.10.1

V1.10.1 is a focused IM dispatch correctness patch on top of V1.10.0.

## Fix 1: URL-prefixed WeLink media embeds

Production WeLinkBot traffic can encode images as either:

```text
/:um_begin{|Img|...}/:um_end
/:um_begin{https://clouddrive...|Img|...}/:um_end
```

The older parser recognized only the first shape. The second shape therefore leaked into human text and, because it begins with `/`, could be mistaken for a WorkBot slash command. V1.10.1 parses the UM payload generically, recognizes known media kinds from the leading fields, strips the protocol placeholder, and preserves the media type metadata.

## Fix 2: alias must start the human-readable text

With `im.intent.require_alias=true`, aliases are no longer searched anywhere in the sentence. After leading whitespace and transport-level non-text media are removed, the first human-readable token must be a configured alias.

Examples:

```text
bot 帮我分析            -> dispatch
   WorkBot /status       -> dispatch
[image] bot 看一下       -> dispatch (image is non-text metadata)
没有bot也会了吗          -> store-only
你好 bot 帮我看          -> store-only
/status                  -> store-only in strict alias mode
```

When `require_alias=false`, legacy slash command routing is unchanged.

## Validation

Focused tests include the exact URL-prefixed image placeholder shape observed in production logs and the sentence `没有bot也会了吗`. Run:

```powershell
.\scripts\check-v1101.ps1
```


---

## 1.10.0

## Multi-message WeLink replies

V1.10 removes the old send-path tail truncation (`answer[-3500:]`, several `result.text[-2000:]`, and workflow `final[-3500:]`). A complete logical reply is now split into multiple durable WeLink messages instead of discarding its beginning.

The splitter guarantees that every physical WeLink message:

- stays within `outbound.welink_max_message_chars` (default `3500`, including native code-block expansion after reserving the bot prefix);
- contains at most one native code block;
- keeps fenced blocks complete;
- preserves message order;
- prefers paragraph/newline/sentence boundaries for long prose;
- re-wraps a single oversized code block into multiple complete fenced blocks, preserving its language tag.

Because V1.9 converts Markdown tables to native monospaced blocks, a reply containing a code example, a table, and a second code example naturally becomes at least three WeLink messages when required by the one-code-block-per-message constraint.

## Durable ordering

All parts of one logical reply are inserted into the durable outbox before the first send. The existing per-conversation send lock is retained. If an earlier part has a transient or ambiguous send failure, later parts stay queued and cannot overtake it. The retry loop now selects only the oldest undelivered row for each conversation.

## Configuration

```json
"outbound": {
  "retry_seconds": 15,
  "retry_max_seconds": 300,
  "welink_max_message_chars": 3500
}
```

The default requires no configuration change for existing installations.


---

## 1.9.0

## WeLink native code-block rendering

WorkBot keeps ordinary Markdown as the reasoning-agent contract, then converts fenced code blocks immediately before WeLink delivery. The formatter emits the native code-block representation observed from `welink-cli im query-history-message`, including the hidden start/end sentinels and the `lang`, `lineBreak`, and `totalLines` metadata object.

WeLink-supported languages are emitted directly: `c`, `cpp`, `css`, `go`, `html`, `java`, `javascript`, `python`, `rust`, `sql`, `typescript`, and `xml`. Common aliases such as `c++`, `py`, `js`, `ts`, `rs`, and `json` are normalized. Shell/PowerShell/plain-text fences use a supported monospaced fallback without changing the code body.

## Tables and diagrams

Markdown tables outside fenced code blocks are converted to Unicode box tables before transport. Column padding uses terminal display width rather than Python character count, so Chinese and ASCII cells remain aligned in the monospaced WeLink block.

ASCII diagrams and command transcripts should be emitted by the Agent inside fenced code blocks. The Agent prompt now states this formatting contract explicitly while keeping WeLink's private hidden metadata out of the model-facing interface.

## Compatibility

Formatting happens only in `WeLinkAdapter`. Conversation history and durable outbox rows keep normal Markdown, so CodeAgent context, retries, and debugging remain readable.


---

## 1.8.2

## Precise single-session stopping

`/stop-session agent-...` now performs a bounded confirmation wait (2 seconds) after requesting cancellation. If the exact invocation leaves the scheduler registry during that interval, WorkBot reports `已停止`; otherwise it explicitly reports that OS process cleanup is still `stopping`. The command remains a fast-control operation and never consumes a CodeAgent slot.

The command still stops exactly one invocation. It does not silently cancel unrelated maintenance/workspace/memory calls. If another invocation belongs to the same Conversation, the acknowledgement names it separately.

## Foreground/background runtime clarity

`/sessions` marks each runtime call as `[FG]` or `[BG]`. Conversation/workflow work is foreground. `maintenance-summary`, `maintenance-memory`, workspace analysis and memory verification are background. Conversation summaries/memory extraction now carry the originating `conversation_id`, so runtime ownership is visible.

Fast control messages no longer trigger opportunistic conversation summarization. `_maybe_schedule_summary()` also refuses to start a summary while any live CodeAgent invocation is already attached to that Conversation. Deferred low-priority maintenance performs the summary later.

Operator cancellation of background summary/memory work is normal control flow and is logged at INFO instead of emitting failure tracebacks.

## Validation

Adds V1.8.2 regression coverage for bounded single-stop confirmation, Conversation-tagged maintenance calls, same-Conversation summary deferral, and fast-control summary suppression.


---

## 1.8.1

## Session-stop correctness

`/stop-session` now changes active invocations to `stopping` immediately. A stopping invocation is visible in `/sessions`, does not count as user-visible active work, and cannot be cancelled a second time while subprocess cleanup is in progress. The scheduler slot is released only after cleanup completes.

CodeAgent cancellation terminates the full process tree. Windows uses `taskkill /T /F` because killing only a `.bat`/`cmd.exe` wrapper can leave the real CodeAgent child alive with inherited pipes, causing `proc.wait()` to hang. POSIX launches use a dedicated process group. Cleanup is shielded against repeated cancellation.

Operator-stopped conversation calls are silent instead of sending `CodeAgent 调用失败`. The `/stop-session` control reply is the only user-facing acknowledgement.

## Durable-outbox race fix

Immediate replies are inserted as `sending`, preventing the background retry loop from concurrently sending the same `send_id`. Retries atomically claim `pending -> sending`; ambiguous/transient failures return to `pending`; successful sends become `delivered`. On startup, interrupted `sending` rows are returned to `pending` so retry can verify history first.

## Validation

Adds regression coverage for stopping-state visibility/idempotence, subprocess-group reaping, silent operator stop, outbox atomic claim, and startup recovery of interrupted sends.


---

## 1.8.0

## Operator SELF-DM dispatch

WeLinkBot reports manually sent direct messages with `isMine=true`. V1.8 stores these messages as `SELF` exactly as before, but no longer drops them before dispatch. In strict alias mode:

- `我晚点回复你` -> ingested/store-only
- `bot 查询xxxx` -> WorkBot turn
- `/sessions` -> WorkBot fast control command

WorkBot-generated outgoing echoes are still filtered by the WeLink adapter before they reach dispatch.

## Live CodeAgent sessions

New operator-only, fast-lane commands:

- `/sessions` — list current active/waiting CodeAgent invocations.
- `/stop-session agent-...` — stop one active or waiting invocation.
- `/stop-sessions all` — stop all current invocations; exact `all` is required.

Runtime IDs (`agent-*`) identify one scheduler invocation, not a durable CodeAgent conversation UUID. Stopping an invocation does not clear the persistent conversation session.

The session list includes purpose, conversation, CodeAgent session prefix, asyncio task name, and elapsed run/wait time. Long-running active calls are marked after 15 minutes.

## Scheduler hardening

The old scheduler maintained a standalone active counter. A cancellation during the narrow waiter->active handoff could theoretically strand a slot and leave `active=max_concurrent` forever. V1.8 derives counts from a live invocation registry and explicitly handles cancellation during handoff. Active invocation cancellation targets the child CodeAgent task/process so the owning conversation worker remains alive.


---

## 1.7.1

V1.7.1 is a Windows compatibility/hardening release for V1.7 remote Workspace Knowledge.

## Fixes

1. **`configure-node-workspace.ps1` now persists the intended node.** PowerShell variable names are case-insensitive; V1.7 used `$Node` for the node name and `$node` for the node config object, so the latter overwrote the former. V1.7.1 uses distinct `$NodeName` and `$nodeCfg` variables, explicitly writes the updated object back to `nodes.<name>`, and re-reads `local.json` after compaction to verify `workspace_knowledge_roots` survived.
2. **Remote virtual paths round-trip on Windows.** A Windows test project such as `C:\\Users\\...\\demo` is represented as `ssh://linux-server1/C:/Users/.../demo` in the unified index, then restored to `C:/Users/.../demo` before the worker helper is called. Linux paths remain unchanged (`ssh://linux-server1/home/workbot/...` -> `/home/workbot/...`).
3. **Config compaction behavior is clarified.** Bounds equal to V1.7 runtime defaults are intentionally omitted; configured roots are never a default and must remain. The configuration script now prints the persisted roots and fails if they are missing.

## Upgrade

Overlay the V1.7.1 update, then run:

```powershell
cd D:\code\workbot
.\scripts\setup-windows.ps1
.\scripts\check-v171.ps1
```

Because the failed V1.7 configuration did not persist the remote roots, configure each affected node again and redeploy it:

```powershell
.\scripts\configure-node-workspace.ps1 -Node linux-server1 -Root "/data/workbot/code","/home/workbot" -ProjectDiscoveryDepth 2 -IndexPathDepth 5 -MaxProjects 20 -MaxFilesPerProject 1200 -MaxChunksPerProject 160
.\scripts\install-node.ps1 -Node linux-server1

.\scripts\configure-node-workspace.ps1 -Node linux-server2 -Root "/data/workbot/code","/home/workbot" -ProjectDiscoveryDepth 2 -IndexPathDepth 5 -MaxProjects 20 -MaxFilesPerProject 1200 -MaxChunksPerProject 160
.\scripts\install-node.ps1 -Node linux-server2
```

With those exact default bounds, compact `local.json` is expected to retain the roots while omitting the redundant `workspace_knowledge` limits.


---

## 1.7.0

V1.7 extends V1.6 Workspace Knowledge to configured Linux nodes while keeping remote indexing bounded and read-only.

## Remote workspace knowledge

Each node may define `workspace_knowledge_roots` and optional per-node limits. The Linux worker performs project discovery, path-depth filtering, sensitive-file exclusion and incremental file reads locally. Windows receives only a bounded manifest and changed chunks through the existing persistent SSH channel.

Remote paths are indexed as `ssh://<node>/<native-path>` and enter the same FTS, project-summary Memory and Agent retrieval pipeline as Windows files.

Important limits:

- `project_discovery_depth` — project-root discovery depth;
- `index_path_depth` — maximum indexed path depth inside each project;
- `max_projects` — maximum project roots per node;
- `max_files_per_project` — bounded file metadata;
- `max_chunks_per_project` / `max_index_chars_per_project` — bounded content transferred to Windows.

The worker independently enforces roots copied into `~/.workbot/config.json`; changing roots requires `install-node.ps1 -Node <name>` so the worker authorization boundary stays synchronized.

## Fair maintenance

When both local and remote projects are due, maintenance alternates local/remote sources. Offline nodes are skipped. Large local roots therefore cannot permanently starve server knowledge updates.

## Search ranking

Configured root order now contributes to workspace ranking. A specific current root such as `D:\code\workbot` is preferred over archived/extracted WorkBot copies under a later root such as Downloads when both match the same query.

## Compact local.json

`setup-windows.ps1` and configuration scripts now run a conservative config compactor. It removes only values that exactly equal hard-coded runtime defaults, preserves unknown/security/user-specific settings, and writes short scalar arrays on one line. Standard node defaults (`relay_command`, `max_concurrent=4`, `priority=100`, `enabled=true`) are also omitted; runtime behavior is unchanged.

Manual compaction:

```powershell
.\scripts\compact-config.ps1
```

## Deployment

Windows files changed and the Linux worker gained bounded workspace RPC methods, so configured remote workspace nodes must be redeployed:

```powershell
.\scripts\setup-windows.ps1
.\scripts\configure-node-workspace.ps1 -Node linux-server1 -Root /home/workbot/code
.\scripts\install-node.ps1 -Node linux-server1
.\scripts\check-v17.ps1
.\scripts\run-workbot.ps1
```

Repeat configuration/install for each node that should expose workspace knowledge.


---

## 1.6.1

Small portability/packaging hotfix on top of V1.6.0.

## Fixes

- Workspace `relative_path` values are now stored with `/` separators on every OS. Absolute filesystem paths remain native Windows paths.
- Existing V1.6.0 workspace rows containing `\` are normalized automatically when the database opens.
- Unchanged indexed files also converge to the normalized relative path on the next scan.
- The full release ZIP is now flat: extracting it directly into `D:\code\workbot` no longer creates an extra `workbot_v1_6_x` directory.
- Explicit fast control commands (`/status`, `/approve`, `/steer`, etc.) intentionally remain able to overtake an ordinary CodeAgent reply in the same conversation. This preserves the control-plane fast lane and is not a FIFO regression.


---

## 1.6.0

## Interaction policy

- Added `im.intent.require_alias` strict response mode.
- Ordinary group **and private** messages require one configured `bot_aliases` token before WorkBot can reply or execute.
- Explicit slash commands do not require an alias.
- Alias gating is dispatch-only: `ingestion_policy` remains authoritative and all allowed messages are still persisted in Conversation Store.
- Matched aliases are stripped from the dispatch copy before command/Agent processing, with path/identifier-safe matching.

## Local workspace knowledge

- Added an incremental read-only index over `workspace_knowledge.roots` or existing `rpc.windows_read_roots`.
- Project discovery uses common repository/build/documentation markers.
- Safe text/code files are chunked into SQLite and FTS5 when available; obvious secrets, local WorkBot config/state, build/cache/vendor directories are excluded.
- Low-priority CodeAgent maintenance creates a compact project purpose/architecture summary and mirrors it into global Memory using a stable source key.
- Interactive Agent context automatically searches the workspace index for relevant project roots/files.
- Background scans/analysis run only while the Agent scheduler is idle and either the configured idle grace has elapsed or the night window is active.
- Added `/workspace` and `/workspace <query>` diagnostics.

## Upgrade

```powershell
cd D:\code\workbot
.\scripts\setup-windows.ps1
.\scripts\configure-v16.ps1
.\scripts\check-v16.ps1
.\scripts\run-workbot.ps1
```

No Linux worker redeploy is required.


---

## 1.5.3

Media-only IM guard for WeLinkBot/CLI-history.

- Detects WeLink UM media placeholders such as `/:um_begin{|Img|...}/:um_end`.
- Pure image/file/audio/video events are stored as Conversation context but never dispatched to intent classification or CodeAgent.
- Mixed media + text keeps only the human-readable text for downstream routing.
- `fileList`-only Hook events are classified as media-only.
- CLI-history TEXT_MSG placeholders receive the same guard.
- No config migration and no Linux worker redeploy are required.


---

## 1.5.2

V1.5.2 hardens realtime WeLink message identity after reconnect/backfill testing exposed duplicate control-message delivery.

## Fixed

- Cross-transport duplicates are suppressed even when WeLinkBot Hook and `query-history-message` expose different message IDs for the same message.
- Added durable `messages.transport`, `messages.dedup_key` and `messages.content_key` with automatic SQLite migration.
- Added exact logical fingerprint plus narrow cross-transport timestamp/content fallback for Hook/API skew.
- Duplicate history aliases still advance fallback cursors.
- Duplicate `/approve` commands no longer start a second CodeAgent resume.
- Added persistent one-shot approval `resume_state` / `resumed_at` guard.
- Hook text normalization now handles plain text, XML/CDATA, HTML fragments and JSON-like card payloads more defensively.
- Direct-message display names upgrade from account ID when the peer later sends an inbound message with a name.

## Upgrade

Windows-only control-plane update. No Linux worker redeploy and no WeLinkBot reconfiguration are required. Existing `config/local.json` and `state/workbot.db` are preserved; new database columns are migrated automatically on startup.

Run:

```powershell
.\scripts\setup-windows.ps1
.\scripts\check-v152.ps1
.\scripts\run-workbot.ps1
```


---

## 1.5.1

V1.5.1 fixes unnecessary CLI history polling after WeLinkBot WebSocket has already reconnected.

## What changed

- Reconnect recovery is now a **bounded backfill sweep**, not a fixed 120-second hot polling window.
- On connect/reconnect WorkBot performs one fresh `query-recent-conversation`, freezes those recent conversations as the recovery target set, and queries each target at most once.
- While the bounded sweep drains, `query-recent-conversation` is not repeatedly called every 3 seconds.
- As soon as all recovery targets have been queried successfully, WorkBot immediately returns to healthy realtime mode and schedules the next CLI reconciliation for the normal low-frequency interval (default 300s).
- The existing 120-second setting is retained as a **maximum sweep duration / safety deadline**, not a minimum polling duration. Existing V1.5 configs using `backfill_after_connect_seconds` remain compatible; new configs use `backfill_max_seconds`.
- If WebSocket is actually disconnected, normal 3-second CLI fallback polling remains active.
- `/welink` now reports bounded backfill active/pending/deadline state.

## Expected behavior

After reconnect with 11 recent conversations, logs may show one recent-conversation discovery and up to 11 paced history queries (each conversation once). When the sweep completes, history polling stops until the next low-frequency reconciliation unless WebSocket disconnects again.


---

## 1.5.0

V1.5.0 changes inbound WeLink IM from polling-first to **WeLinkBot WebSocket push-first with CLI-history recovery**.

## Added

- receive-only `WeLinkBotReceiver` with local WebSocket auth and automatic reconnect;
- normalization for real Hook group/private/self payloads;
- nested XML/CDATA message extraction plus `showText` fast path;
- realtime `isAt` propagation for immediate directed-to-Bot routing;
- owner manual DM SELF ingestion without auto-reply;
- WorkBot outbound Hook-echo suppression;
- push/history shared durable dedup path;
- numeric message-id plus server-time recovery cursors;
- startup/reconnect backfill and automatic polling fallback;
- low-frequency CLI reconciliation while WebSocket is healthy;
- `/welink` realtime connection, queue, reconnect and fallback diagnostics;
- `websockets` Python runtime dependency;
- `configure-v15-welinkbot.ps1`, `check-v15.ps1`, and `docs/realtime-welink.md`.

## Default transport behavior

- healthy WebSocket: immediate push receive, CLI reconcile every 300s;
- first connect/reconnect: 120s CLI backfill window;
- WebSocket unavailable: CLI polling fallback every 3s;
- CLI history still shares the 18/min WorkBot budget under the documented 20/min limit.

## Upgrade

Stop WorkBot, overlay the V1.5 update, then:

```powershell
.\scripts\setup-windows.ps1
.\scripts\configure-v15-welinkbot.ps1
.\scripts\check-v15.ps1
.\scripts\run-workbot.ps1
```

Start your external WeLinkBot executable separately. It is intentionally not shipped with WorkBot. No Linux worker redeploy is required.

For local testing an empty secret is allowed on `ws://127.0.0.1:4080`. Prefer setting a WeLinkBot secret and supplying it through `WORKBOT_WELINKBOT_SECRET` for normal use.


---

## 1.4.1

Stability and responsiveness patch for V1.4.0.

## Fixes

- Fixes the Windows-only Gateway tests: the fake WeLink executable is now a `.cmd` shim on Windows and the managed Gateway can explicitly launch `.cmd/.bat` through `cmd.exe`.
- `query-recent-conversation` is now intended to run on the fast IM poll cadence (default 3 seconds instead of 60 seconds).
- Recent conversations are treated as activity hints. The top recent conversations are pulled into the high-priority history queue without bypassing the documented 20/min history limit.
- Important groups, private conversations and conversations awaiting a reply/approval are prioritized ahead of overdue quiet rooms.
- Any WorkBot reply temporarily boosts that conversation (default 120 seconds), making `/approve`, confirmations and follow-up answers much more responsive.
- `/welink` diagnostics now show discovery cadence, recent-focus size and reply-boost duration.
- Approval resume logs now record CodeAgent continuation start/finish duration, making it possible to separate Agent latency from Gateway/CLI latency.

## Rate-limit model

`query-recent-conversation` is used as a lightweight activity/discovery signal. `query-history-message` remains globally limited to an 18/min WorkBot budget under the documented 20/min ceiling. Fast discovery does not increase the history API budget.


---

## 1.4.0

V1.4 promotes the installed `welink-cli-tool` skill into a governed WorkBot capability instead of allowing Windows CodeAgent subprocesses to call WeLink outside the control plane.

## New

- WeLink capability registry covering IM, meetings, contacts/search, OneBox, mail and calendar.
- Reply Policy gets read-only WeLink research capabilities.
- Execution/Action Policy controls WeLink writes with stable actions such as `email.send`, `meeting.write`, `cloud.write` and `im.send`.
- Windows CodeAgent PATH is scoped so `welink-cli` resolves to a WorkBot gateway wrapper only inside Agent subprocesses.
- WorkBot polling/sending and CodeAgent skill calls share one cross-process WeLink CLI gate.
- IM history calls share one persistent 18/20-per-minute budget across WorkBot and Agent calls.
- OneBox Agent calls are paced per interface at at least one second.
- Agent-initiated IM sends are paced using the FAQ's 30-second recommendation.
- One-use approval token: one approved WeLink action permits one matching write CLI invocation.
- `/welink-tools` operator diagnostic.

## Policy

`auth login`, `auth logout` and CLI config mutation are not automated. WorkBot continues to rely on the logged-in WeLink PC and CLI token refresh.

## Upgrade

This is Windows-control-plane only; Linux workers do not need redeployment.

```powershell
.\scripts\setup-windows.ps1
.\scripts\configure-v14.ps1
.\scripts\check-v14.ps1
.\scripts\run-workbot.ps1
```


---

## 1.3.0

V1.3 turns a managed Task from a one-shot CodeAgent invocation into a steerable, multi-turn execution entity while preserving the task ID and CodeAgent session.

## Task Steering

Remote active tasks support:

```text
/steer task-<id> <new authoritative correction>
/add task-<id> <follow-up instruction>
```

- `steer` immediately supersedes the current turn, terminates its managed process group, then resumes the same CodeAgent session with a new turn.
- `add` queues the instruction and resumes the same session only after the current turn completes.
- each instruction is recorded in `task_instructions` with sequence/mode/state/timestamps;
- stale output/events from a superseded turn are rejected by turn number and cannot complete or resurrect the task;
- `/continue` keeps its older meaning for terminal tasks: create follow-up work rather than steering an already-finished execution.

Windows Workflow steps support the analogous forms:

```text
/steer wf-<id> step-N <correction>
/add wf-<id> step-N <follow-up>
```

The workflow step remains the same; only the current Windows CodeAgent invocation is interrupted/resumed.

## Linux process-scope lifecycle

V1.3 remote turns run through `setsid` and record their process-group leader. `/steer` and `/cancel` terminate the active process group (TERM, grace period, then KILL if necessary) before proceeding. Node installation now checks `codeagent`, `tmux` and `setsid` explicitly.

This controls ordinary descendant processes but is not a security sandbox against deliberate daemon/session escape.

## WeLink query-history limit

Q17 documents `query-history-message` at 20 calls/minute. V1.3 adds a sliding 60-second global history-query budget:

- documented limit: 20/min;
- default internal budget: 18/min;
- burst: one;
- default effective spacing >= 60/18 seconds;
- 429 cooldown remains enabled.

Use `scripts/configure-v13.ps1` to write these recommended values to an existing configuration.

## Database migration

Windows `tasks` gains `agent_session_id`, `current_turn` and `active_instruction_seq`; a new `task_instructions` table stores instruction history. Linux worker SQLite receives corresponding turn/runtime metadata. Migrations are in-place; do not delete existing databases.

## Upgrade

```powershell
.\scripts\setup-windows.ps1
.\scripts\configure-v13.ps1
.\scripts\install-node.ps1 -Node linux-server1
.\scripts\install-node.ps1 -Node linux-server2
.\scripts\check-v13.ps1
```

Redeploy all Linux workers because worker runtime, `agentctl`, process-scope handling and turn metadata changed.


---

## 1.2.1

## Fixed

### SQLite migration
V1.2.0 background conversation-memory maintenance referenced `conversations.memory_message_count`, but V1.2.0 accidentally omitted that column from both the initial schema and upgrade migration. V1.2.1 adds it to both paths. Existing `state/workbot.db` files are migrated in place on startup.

### WeLink 429 / dynamic-ingestion fan-out
V1.2.0 dynamic discovery could discover 10-20 recent conversations and then immediately issue one `query-history-message` command per due conversation. The V1.0.1 CLI serialization gate prevented concurrency, but did not pace sequential calls, so the burst could still trigger WeLink 429 limits.

V1.2.1 adds:

- global history-query pacing (default 3 seconds between history calls);
- normally one history query per WorkBot poll tick;
- direct/private and configured important groups win scheduling ties;
- oldest-due scheduling prevents ambient groups from starving;
- explicit 429 detection;
- first 429 immediately stops the current fan-out and activates a 30-second global history cooldown;
- send-side 429 is durably retried without immediately issuing a history verification query into the same rate-limit window;
- `/welink` diagnostics include rate-limit hits and remaining pacing/cooldown time.

The documented 30-second rule supplied for WeLink sending is not assumed to be the exact history-query quota. The history endpoint demonstrably returns 429 as well, so V1.2.1 uses conservative pacing plus adaptive cooldown instead of treating the send limit as a universal fixed rule.

## Upgrade

Windows-only update. No linux-server1/linux-server2 reinstall is required.

```powershell
.\scripts\setup-windows.ps1
.\scripts\configure-v121-welink.ps1
.\scripts\check-v121.ps1
```


---

## 1.2.0

V1.2 turns WeLink from a test-group input into a broad conversation ingestion layer without allowing ambient chat to consume unlimited CodeAgent capacity or trigger unsafe replies.

## New

- Dynamic WeLink discovery of recent group and private conversations (`im.discovery.mode=all`).
- `im.groups` retained as static seed/backward compatibility, not a discovery-mode ingestion limit.
- Group intent routing: direct @/aliases immediate; ordinary group messages cheap-filtered, delayed, batched and conservatively classified.
- Important-group shorter batching delay and global CodeAgent priority scheduling.
- Private read-only feasibility gate: Reply Policy users get an answer only when WorkBot can materially solve the request without execution/side effects; otherwise silent.
- Explicit Ingestion / Reply / Execution policies.
- Reply-only users cannot inspect WorkBot control-plane commands/state.
- Operator manual private replies stored as `SELF` context and never auto-replied to.
- Conversation-local automatic memory candidates + idle/night evidence-based promotion to global memory.
- Framework-rendered authoritative workflow target line/headline for multi-node proposals.
- `/welink` diagnostics include discovered-conversation and scheduler state.

## Reliability fix

Late task-scoped notifications are now suppressed at both layers:

- Linux worker drops `task.progress`, `task.blocked`, `agent.notification` and other task-scoped events when the local task is already terminal, returning an ACK with `dropped=true` and never inserting them into durable outbox.
- Windows TaskManager ignores late events for tasks already `completed`, `failed` or `cancelled`, preserving the terminal state and not routing the event to WeLink.

This addresses background timers/processes left behind by an older stopped task continuing to notify WorkBot later.

## Memory safety

Conversation-derived automatic extraction can never write global memory directly, even if an old config still contains `memory.auto_global=true`. Global promotion requires a separate low-priority verifier and high-confidence evidence/corroboration.

## Upgrade

V1.2 modifies both Windows and Linux code.

```powershell
.\scripts\setup-windows.ps1
.\scripts\configure-v12-im.ps1 -OwnerAccount owner-account -BotAliases "@测试用户","WorkBot" -ImportantGroups "1001"
.\scripts\check-v12.ps1
.\scripts\install-node.ps1 -Node linux-server1
.\scripts\install-node.ps1 -Node linux-server2
.\scripts\run-workbot.ps1
```

Do not overwrite `config/local.json` with the example file; use the configure script so existing node/RPC/action settings are retained.

## Validation

The V1.2 source tree passes 108 automated tests, including dynamic group/DM discovery, private read-only silence/answer behavior, group intent batching, priority scheduling, memory promotion candidates and two-layer stale-task notification suppression.


---

## 1.1.0

V1.1 fixes a control-plane weakness exposed by a two-node deployment: an LLM could know that `linux-server1` and `linux-server2` existed yet still answer from history or create work on only one node.

## Deterministic Scope Resolver

`workbot/orchestration/scope.py` resolves explicit execution scope before the Conversation Agent:

- `所有节点 / all nodes` → `office-pc` + every enabled remote node.
- `所有Linux节点 / 所有服务器` → all enabled Linux remote nodes.
- x86 / ARM / GPU all-node expressions → Node Registry capability/label/routing-hint matches.
- explicit Windows/remote node names → exact target set.

Scope resolution does not invent task categories. It only determines *where* the user explicitly requested execution.

## Multi-target means Workflow

If an executable request resolves to more than one target, WorkBot bypasses ordinary reasoning-agent routing and directly proposes one Workflow. A reasoning Agent that nevertheless returns `remote_task` for a multi-target instruction is overridden to Workflow.

One request produces one confirmation. Remote child tasks are internal Workflow steps and do not ask for per-node creation confirmation.

## Planner coverage validation

The Workflow planner now accepts framework-resolved `required_targets`. Parsed plans must cover each target:

- `office-pc` → at least one `executor=windows` step.
- remote target → at least one `executor=remote,node=<exact target>` step.

If a target is missing, WorkBot asks the planner to repair the valid-but-incomplete plan once. If coverage is still incomplete, the plan is rejected rather than shown to the user.

## Fresh execution semantics

Read/query/check/current-state requests carry `fresh_execution=true`. Conversation summaries, memory and historical task results remain useful context, but they cannot be presented as the current result instead of executing the requested operation on every target.

## Atomic PendingAction consumption

Confirmation/cancellation words are now read and clear `pending_action_json` in one SQLite transaction before any reasoning-agent path. Duplicate delivery cannot execute a proposal twice, and `否/取消/创建/执行` cannot become a second free-form task while consuming a proposal.

If task/workflow creation fails after atomic consumption, the proposal is restored so the user can retry.

## Offline required targets

A required but offline node is shown in the Workflow proposal as a warning. WorkBot does not silently reroute a user-explicit target to another node.

## Deployment

V1.1 changes only the Windows control plane. Existing V1.0 workers on linux-server1/linux-server2 do not need reinstalling.


---

## 1.0.1

## WeLink CLI serialization and diagnostics

V1.0 introduced durable outbound retries, but polling, sending, send-verification and retry could still launch separate `welink-cli` processes concurrently through different asyncio/thread-pool paths.  The WeLink PC/helper verification channel appears sensitive to that concurrency and may return `WebSocket: Verification timeout` even while a manually executed one-shot CLI command succeeds.

V1.0.1 routes every WorkBot-owned WeLink operation through one adapter-wide operation gate.  One complete operation runs at a time:

- group history polling;
- `send-to-group`;
- history verification after an ambiguous send;
- durable outbox delivery retry.

The existing durable outbox and verify-before-retry behavior remain unchanged.

## CLI trace

Enable temporary detailed tracing:

```powershell
.\scripts\configure-welink-trace.ps1
```

Restart WorkBot.  Logs include operation type, queue wait, duration, return code and a payload-safe command representation.  Message bodies passed to `--text` are replaced with their length and SHA-256 prefix.

Disable after diagnosis:

```powershell
.\scripts\configure-welink-trace.ps1 -Disable
```

Use `/welink` in an authorized WeLink conversation to inspect current adapter counters, last operation, last return code, execution/wait duration and poll-backoff state.

V1.0.1 does **not** call `welink-cli auth login` on verification timeout.  The logged-in WeLink PC/client remains the source of authentication and token refresh; transient verification failures are handled as local transport failures.


---

## 1.0.0

V1.0 turns the V0.x linux-server1-centered implementation into a capability-aware multi-node control plane and fixes three reliability issues found during V0.9 validation.

## Multi-node Node Registry

- Static node capability/label/routing metadata in `config/local.json`.
- Runtime SSH online/offline state plus active-task/capacity load.
- `/nodes` fast-lane command.
- Windows Conversation Agent receives the live node catalog and may use `node=auto`.
- Workflow planner receives node online/load/capability metadata.
- Generic `configure-node.ps1` and `install-node.ps1`; `install-linux-server1.ps1` remains a compatibility wrapper.
- `setup-windows.ps1` no longer hard-codes an SSH check to linux-server1.

## Linux managed-task isolation

Each remote task gets a durable independent CodeAgent UUID and is started with `--session-id`. The default managed Agent cwd is the task's own `~/.workbot/tasks/<task-id>` directory, with node AGENTS.md staged into it. This prevents unrelated tasks from inheriting implicit CLI/cwd context. Worker recovery reattaches the original tmux/session rather than creating a new one.

## Durable/ambiguity-safe WeLink replies

A WeLink `Verification timeout` during send is treated as an ambiguous transport failure, not as an Agent failure and not as a reason to run `auth login`. WorkBot:

1. durably stores the outgoing reply;
2. after an ambiguous send, checks recent WeLink history for the exact reply/time window;
3. if already present, records success without resending;
4. otherwise leaves it in a persistent outbox and retries with exponential backoff, checking history before retry.

## Cross-platform tests

Unix-domain-socket integration and real bash-wrapper execution tests are Linux-only and are skipped on Windows. Windows continues to run protocol/unit/static tests; `check-v10.ps1` additionally checks each configured Linux worker over SSH.


---

## 0.9.0

V0.9 focuses on **Linux worker recovery** and **managed-task command gates**.

## Worker recovery

`agent-worker` now rescans non-terminal task rows on daemon startup.

- If the deterministic tmux session still exists, the new worker reattaches its monitor instead of launching a second Agent.
- If the CodeAgent finished while the worker was down and `exit_code` exists, the new worker finalizes the task and emits the durable terminal event.
- If a recorded running task has neither a tmux session nor a result file, it is marked failed with `WorkerRecoveryLostTask` instead of remaining permanently `running`.
- Terminal event IDs are deterministic (`task_id + terminal state`) so repeated worker restarts cannot generate duplicate completion/failure notifications.
- Terminal task state + durable outbox event are committed in one SQLite transaction, eliminating the crash window where a task could become terminal but its final event was never queued.
- Linux worker SQLite adds `recovery_count` and `last_recovered_at`.
- `agentctl worker-status` exposes node-local task/recovery state without needing Windows RPC.

Stopping/restarting the worker does **not** kill managed tmux sessions. `install-linux-server1.ps1` therefore upgrades the daemon while allowing active tasks to survive and be rediscovered by the new worker.

## Managed-task command gates

Managed CodeAgent tasks now prepend:

```text
~/.workbot/enforced-bin
```

to `PATH`.

The shipped wrappers automatically route these commands through WorkBot approval:

- `git push` / force-push
- `git merge`
- `git reset --hard`
- `git clean -f...`
- `sudo ...`

The generic interface is:

```bash
agentctl gated-exec \
  --action deploy.production \
  --summary "Deploy service X" \
  -- ./deploy.sh production
```

`gated-exec` sends WorkBot the **actual argv and cwd**, waits for policy/approval, and if approved executes that exact argv. It then writes a durable `policy.action.executed` audit event containing approval id, argv, cwd and exit code. That audit event is stored but intentionally not sent as noisy IM progress.

New approval-required action names include `git.reset_hard` and `git.clean.force`.

## Enforcement boundary

This is stronger than a prompt-only convention but still not a kernel sandbox. A same-user Agent could deliberately bypass a PATH wrapper with an absolute binary path or directly access credentials. Full mandatory enforcement requires a restricted execution identity/container plus credential separation, with privileged credentials/actions reachable only through a controlled helper/service.

## Upgrade

V0.9 changes Linux worker code, `agentctl`, Linux AGENTS/skills and installs `enforced-bin`; run `install-linux-server1.ps1` after covering the Windows update.

Validate with:

```powershell
.\scripts\check-v09.ps1
```

With WorkBot running, a safe approval+execution diagnostic is:

```powershell
.\scripts\test-v09-gated-exec.ps1
```

The diagnostic asks for `git.push` approval but, after approval, executes only `/bin/echo WORKBOT_V09_GATED_EXEC_OK`.

Automated regression suite: **76 passed**.


---

## 0.8.0

## Added

- Configurable `action_policy` with first-match glob rules: `allow`, `approve`, `deny`.
- Durable `approval_requests` audit table.
- `/approvals`, `/approve approval-...`, `/deny approval-...` plus Chinese aliases.
- Linux `agentctl request-approval` synchronous approval RPC over the existing persistent SSH channel.
- Remote CodeAgent `request-approval` skill and stronger AGENTS.md guidance.
- Windows CodeAgent `<WORKBOT_APPROVAL>` protocol. Ordinary Conversation Agent turns resume the same session after approval; Windows Workflow steps can wait and resume in the background.
- Approval requests are independent from Conversation `PendingAction`, so a task can ask for approval while other task/workflow state exists.
- Startup marks stale pending approvals `interrupted` rather than replaying them.

## Default policy

Ordinary read/search/edit/build/test/local-git work remains low-friction. Push/merge/deploy/external messaging/destructive operations normally require approval. Credential exfiltration/security-disable/destructive-system actions are denied. Unknown actions default to approval.

## Security boundary

This is a control-plane policy and approval protocol, not an OS sandbox. CodeAgent still has the permissions of its Windows/Linux account. Use least-privilege credentials and protected production systems for hard enforcement.

## Upgrade

V0.8 changes Windows WorkBot and the Linux node package (`agentctl`, node AGENTS/skills, managed task prompt), so rerun `install-linux-server1.ps1`.


---

## 0.7.0

V0.7 turns the existing persistent SSH relay into a genuinely bidirectional Agent transport and removes CodeAgent latency from the WeLink polling loop.

## Linux -> Windows synchronous RPC

A Linux CodeAgent/script can now request Windows-side information through its local worker without opening a Windows port or SSHing back to the office PC.

New `agentctl` commands:

```bash
agentctl read-windows 'D:\code\workbot\state\workflow-input.txt'
agentctl list-windows 'D:\code\workbot\state'
agentctl ask-workbot --instruction '查找Windows侧项目说明/Wiki中关于Feature X的约定并返回'
agentctl request --method windows.fs.read --data-json '{"path":"D:\\code\\workbot\\state\\workflow-input.txt"}' --text
```

Worker semantics:

- local request IDs are multiplexed over the existing upstream SSH connection;
- Windows responses are routed back to the exact local Unix-socket client;
- no upstream -> `WorkBotUnavailable` immediately;
- upstream disconnect during request -> `WorkBotDisconnected`;
- requests are synchronous/bounded rather than durable: they are not silently replayed after reconnection.

Windows methods are allow-listed:

- `windows.fs.read` — bounded text file read under configured roots;
- `windows.fs.list` — bounded directory listing under configured roots;
- `workbot.query` — fresh one-shot, read-only Windows CodeAgent query for local/company information.

`workbot.query` never reuses the user's IM CodeAgent session, so a Linux information lookup cannot corrupt or block that session's state. It is explicitly instructed not to send messages/mail, mutate files/memory, create tasks, deploy, or perform another side effect.

Managed remote tasks now receive `WORKBOT_CONVERSATION_ID` in addition to `WORKBOT_TASK_ID`, so `agentctl ask-workbot` can automatically attach the originating conversation context.

## IM concurrency model

The WeLink poll loop now ingests/persists messages and dispatches them; it does not await CodeAgent execution.

- same Conversation: ordinary messages stay FIFO because they share sequential conversation state;
- different Conversations: independent queue workers run concurrently;
- same CodeAgent session: protected by a per-Conversation Agent lock even when background workflow steps are active;
- fast lane: `/status`, `/session`, memory lookup and explicit-ID `/cancel task-...`/`/cancel wf-...` can run while a long ordinary Agent turn is in progress;
- outbound WeLink sends are serialized only for the short send operation per Conversation;
- Task/Workflow execution remains background and independent of IM polling.

Thus a busy group no longer prevents WorkBot from polling new messages or serving other groups, while dialogue order inside that group remains deterministic.

## Security/configuration

RPC is **disabled by default**. Enable trusted nodes/methods explicitly:

```powershell
.\scripts\configure-rpc.ps1 -Node linux-server1 -ReadRoot D:\code\workbot
```

Example config:

```json
"rpc": {
  "enabled": true,
  "allowed_nodes": ["linux-server1"],
  "allowed_methods": ["windows.fs.read", "windows.fs.list", "workbot.query"],
  "windows_read_roots": ["D:\\code\\workbot"],
  "max_response_chars": 16000,
  "max_file_chars": 64000,
  "timeout_seconds": 300,
  "max_concurrent": 2
}
```

`windows_read_roots` is enforced for direct filesystem RPC. `workbot.query` is policy/prompt constrained and intended only for trusted nodes and read-only information retrieval; it is not a generic Windows shell.

## Deployment

V0.7 changes both Windows and Linux worker/agentctl code, so rerun `install-linux-server1.ps1`.

```powershell
.\scripts\setup-windows.ps1
.\scripts\configure-rpc.ps1 -Node linux-server1 -ReadRoot D:\code\workbot
.\scripts\install-linux-server1.ps1
.\scripts\check-v07.ps1
.\scripts\run-workbot.ps1
```

With WorkBot running in another terminal/psmux pane:

```powershell
.\scripts\test-v07-rpc.ps1
```

## Tests

V0.7.0 ships with 61 tests. New coverage includes Windows RPC authorization/root confinement, origin task/conversation metadata, non-blocking Windows RPC dispatch, cross-Conversation concurrency, same-Conversation FIFO, per-session serialization, and Linux worker request/response routing/disconnect behavior.


---

## 0.6.2

Stability patch based on Windows V0.6.1 validation.

## Fixes

- Reworked the Windows WorkBot SQLite `Store` to use short-lived WAL connections. This removes lingering `.db` handles that caused pytest temporary-directory cleanup to fail with `WinError 32`.
- Added `pytest-asyncio` to the `dev` extra so `@pytest.mark.asyncio` tests run after `setup-windows.ps1` on a clean environment.
- `/summary refresh` now performs a full rebuild without feeding the old summary back into CodeAgent.
- Summary generation requests a `<WORKBOT_SUMMARY>` wrapper and strips common meta commentary if the model ignores the wrapper.
- Strengthened summary rules to omit resolved one-off failures and transient session/install diagnostics.
- Clarified `check-session.ps1`: its test UUID is diagnostic-only; the WeLink Conversation receives its own WorkBot-assigned UUID on the next normal conversational Agent call.

## Upgrade

Windows-only update. Stop WorkBot, overlay the update package, then run:

```powershell
.\scripts\setup-windows.ps1
.\scripts\check-v06.ps1
.\scripts\check-session.ps1
.\scripts\run-workbot.ps1
```

No linux-server1 redeployment is required.


---

## 0.6.1

- WorkBot now assigns a UUID when creating a new conversational CodeAgent session via `--session-id <uuid>` and resumes it with `--sessions <uuid>`. CLI output no longer needs to expose a session ID.
- Conversation summaries now treat database Task/Workflow state as authoritative, remove superseded failures, and avoid turning one-off errors into general reliability conclusions.
- Durable memory no longer auto-captures from conversation summaries by default.
- Auto-memory candidates require verbatim evidence present in the source material, confidence >= 0.85, and only `fact/decision/preference` kinds. Auto-generated global memory is disabled by default.
- Automatic memory rows now record evidence and `auto_generated`; `/forget auto-summary` deletes old summary-derived auto-memory without touching manual `/remember` entries, and `/forget auto` deletes all auto-generated memories.
- Workflow auto-memory now receives raw step results as primary evidence instead of only the synthesized answer.
- WeLink `Verification timeout` / transient WebSocket polling failures enter exponential cooldown instead of producing a full poll-loop traceback every cycle. Other groups are isolated from non-transient per-group failures.
- Linux worker modules import POSIX `fcntl` lazily so Windows diagnostics/tests no longer require `winfcntl`.

- Added `/summary refresh` and `/summary reset` so existing stale summaries can be repaired immediately after upgrade.


---

## 0.6.0

- Added global and conversation memory scopes, kind/source/confidence metadata, fingerprint deduplication, and deletion.
- Added FTS5 search with LIKE fallback.
- Added `/memories` and `/forget` plus scoped `/remember`.
- Added optional Agent-curated automatic capture from workflow results and conversation summaries.
- Memory curator rejects transient task state, one-off file contents, credentials/secrets and sensitive personal information.


---

## 0.5.0

- Persist outgoing WorkBot replies so recent conversation is genuinely multi-turn.
- Track session UUID, turn count, last-use time and allow manual session bind/reset.
- Reuse `codeagent --sessions <uuid>` when a UUID is available.
- Rotate long/stale sessions and rely on summary + recent messages for stable continuity.
- Add asynchronous conversation summarization.
- Add active/recent Task/Workflow context to every reasoning Agent invocation.


---

## 0.4.0

- Added worker protocol `task.cancel` and durable `task.cancelled`.
- Cancellation kills task tmux sessions; Windows workflow cancellation kills local CodeAgent subprocesses.
- Added direct task retry, child-task continuation, workflow-step retry, and workflow resume.
- Added persisted workflow restart recovery.
- Safety rule: a Windows workflow step that was running when WorkBot died becomes `interrupted`; it is never silently rerun after restart.


---

## 0.3.2

V0.3.2 fixes the Windows `codeagent.bat` prompt transport used by Workflow planning.

## Root cause

The planner code did include the current user request, but the full prompt was passed as a large multiline `-p` argument to `codeagent.bat`. Windows batch/cmd argument handling is not a reliable transport for long multiline prompts. In the observed failure, CodeAgent saw the planner role / workspace context but not the trailing concrete user request, then asked "What request would you like me to plan for?".

## Fix

- On Windows, when CodeAgent resolves to `.bat`/`.cmd` and the prompt is multiline/large, WorkBot writes the actual UTF-8 prompt to `state/runtime-prompts/prompt-*.txt`.
- The CLI receives only a short one-line bootstrap prompt telling CodeAgent to read that file completely first.
- The temporary prompt is removed after the CodeAgent process exits; stale prompt files older than 24 hours are cleaned opportunistically.
- Workflow planner places the authoritative current user request at the beginning of its prompt.
- The one-shot structured-output repair retry repeats the exact original user request and explicitly forbids asking what the task is.
- Short single-line prompts such as `codeagent -p "hello"` continue using normal argument transport.

## Upgrade

Windows only. No linux-server1 redeployment is required.

After overlaying the update, run:

```powershell
.\scripts\setup-windows.ps1
.\scripts\check-workflow-planner.ps1
```

Then restart WorkBot.


---

## 0.3.1

## Fixed

- Workflow planner no longer assumes the full CodeAgent stdout is pure JSON.
- Accepts `<WORKBOT_PLAN>...</WORKBOT_PLAN>`, JSON markdown fences, JSON surrounded by CLI prose, and ANSI-decorated output.
- If the first planner response is invalid/empty, performs exactly one repair retry with a strict output-only prompt.
- Invalid raw planner outputs are logged for debugging; WeLink receives a concise error instead of `JSONDecodeError: line 1 column 1`.
- Adds `scripts/check-workflow-planner.ps1` for direct planner diagnosis.

## Upgrade

This is Windows-side only. Stop WorkBot, extract the update over `D:\code\workbot`, then restart:

```powershell
.\scripts\run-workbot.ps1
```

No `install-linux-server1.ps1` is required when upgrading from V0.3.0.


---

## 0.3.0

## Main changes

1. Generic task semantics: WorkBot no longer classifies tasks as development/test/build/investigation. Remote execution is `codeagent` plus an instruction.
2. Cross-node Workflow DAG: Windows-local and one/more Linux-node CodeAgent steps can run in one confirmed request and have their results synthesized.
3. Agent-selected milestone notifications: planner-selected step milestones plus remote `task.progress/task.blocked`; routine updates remain silent.
4. WeLink inbound ACL: sender/group allowlist and denylist with deny precedence; recommended `default=deny` while WorkBot is experimental.
5. Structured reasoning-Agent action proposals: CodeAgent may recommend a generic remote task or Workflow; WorkBot still owns PendingAction confirmation.

## Required local config action

The update archive intentionally does not overwrite `config/local.json`. To restrict WorkBot to your account:

```powershell
.\scripts\configure-access.ps1 -Account owner-account
```

## Linux update

Rerun:

```powershell
.\scripts\install-linux-server1.ps1
```

This updates node AGENTS/skills. Existing worker transport/state remains compatible.


---

## 0.2.0

This release turns the V0.1 acceptance-test path into a generic remote-task path.

## Main changes

1. Explicit remote natural-language commands such as `在linux-server1...` are handled deterministically without first invoking the Windows CodeAgent.
2. The original user instruction is preserved in `PendingAction` and `task.create`; task type/title is metadata only.
3. The V0.1 hard-coded “集成测试任务” prompt/reply has been removed.
4. `/task linux-server1 <instruction>` is added; `/test <instruction>` remains as an explicit test shortcut.
5. Paths/file names such as `/tmp/workbot-test/test.txt` are excluded from task-type classification so the word `test` inside a path cannot change intent.
6. WorkBot-managed Linux CodeAgents no longer need to send their own final `agentctl notify`; agent-worker owns authoritative final completion/failure. Optional `task.progress` and `task.blocked` remain available.
7. Task-scoped duplicate `agent.notification` / `test.completed` / `test.failed` events are suppressed from IM, while manually started Agents without a task id can still notify WorkBot.
8. PowerShell scripts set `$OutputEncoding` to UTF-8 without BOM. Scripts with `param(...)` keep `param` as the first executable statement.
9. Linux worker lifecycle now supports `worker_main.py stop`; `install-linux-server1.ps1` restarts the detached worker after copying an upgrade so the new code is active immediately.

## Upgrade

Overlay the update archive onto `D:\code\workbot`, then rerun the Linux installer because the worker prompt and node-local skills changed:

```powershell
cd D:\code\workbot
.\scripts\install-linux-server1.ps1
.\scripts\run-workbot.ps1
```

No database migration is required. `config/local.json` and `state/workbot.db` are not included in the overlay archive.


---
