# Conversation and global memory (V1.2)

WorkBot separates environment knowledge, conversation state, task/workflow state, and long-term memory.

## Scopes

- `conversation:<conversation_id>`: facts/decisions/preferences grounded in one conversation.
- `global`: verified reusable knowledge suitable across conversations.

All automatic facts extracted from conversation messages **must enter conversation scope first**, even if an old configuration contains `memory.auto_global=true`. Global promotion is a separate verification process.

## Conversation extraction

When enough new conversation messages accumulate, a low-priority maintenance Agent extracts at most a few durable candidates. Every automatic memory needs an exact evidence span present in source material and confidence >= 0.85. Transient task status, rumors, one-off failures, credentials/secrets and sensitive personal information are excluded.

Automatic conversation memories are marked `promotion_state=candidate`.

## Verified promotion to global memory

During idle maintenance cycles, WorkBot periodically reviews conversation candidates. Promotion runs when enough candidates accumulate or during the configured night window. The verifier:

- compares candidates across conversations and against existing global memory;
- may use available read-only files/Wiki/search sources for corroboration;
- rejects transient status, weak single-source claims, conflicts and sensitive data;
- only promotes `fact`, `decision`, or `preference` with confidence >= 0.85.

The global memory records source candidate IDs and evidence, and source candidates are marked promoted/reviewed. Rejected candidates are not immediately re-reviewed on every maintenance cycle.

This gives WorkBot broad workplace awareness without treating every group-chat statement as global truth.

## Maintenance scheduling

Summary, conversation-memory extraction and global fact verification use the lowest CodeAgent scheduler priority and run only while the scheduler is idle. The default global-promotion night window is 01:00–05:00 and can be changed in `memory.promotion_night_start_hour/end_hour`.

## Manual commands

Operator-only commands remain available:

```text
/remember global title | content
/remember conversation title | content
/memory <query>
/memories
/forget <id>
```

## V1.11.0 Hybrid RAG retrieval

Memory remains authoritative in `memory_items`; V1.11.0 mirrors active items into the derived RAG corpus instead of storing vectors in the business table itself. Each memory item is an atomic retrieval document/chunk.

V1.11.1 defaults to cross-conversation Memory retrieval because a single project commonly spans several WeLink groups and private chats. The original `scope` remains stored and shown as provenance, but it no longer blocks a relevant memory from another conversation. To restore V1.11.0 isolation, set `memory.cross_conversation_retrieval=false` for legacy Memory search and `rag.retrieval.memory_scope=conversation` for Hybrid RAG/vector retrieval.

`/memory <query>` now prefers Hybrid RAG (FTS5 + vector RRF) and falls back to the legacy Memory search if the RAG corpus has not yet been synced. `/remember`, `/forget`, automatic extraction and global promotion synchronize the RAG text layer; vector embedding is incremental and may be filled by idle maintenance.

Do not edit `rag_*` tables as if they were Memory. They are rebuildable indexes. See `docs/rag.md`.
