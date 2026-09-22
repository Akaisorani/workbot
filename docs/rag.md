# Local Hybrid RAG

WorkBot V1.11.0 adds a local retrieval layer over Memory, Workspace knowledge, source code, and future document sources. The design deliberately keeps the existing business tables authoritative and treats embeddings/vector indexes as rebuildable derived state.

## Architecture

```text
Memory / Workspace / future document caches
                  |
                  v
      rag_documents (canonical source)
                  |
                  v
          rag_chunks (semantic chunks)
            /                 \
           /                   \
      FTS5/BM25             dense embedding
       lexical                vector index
           \                   /
            \                 /
              weighted RRF
                  |
          metadata/exact boost
                  |
        optional CrossEncoder
                  |
          source diversity
                  |
             RAG context
                  |
              CodeAgent
```

Vector search does **not** replace FTS5. Exact identifiers such as `ExecReScanStream`, GUC names, file paths, SQL snippets and error strings are often better lexical keys; dense retrieval adds paraphrase/cross-language semantic recall.

## Default model and vector backend

The shipped defaults are:

```json
{
  "embedding": {
    "provider": "sentence-transformers",
    "model": "Qwen/Qwen3-Embedding-0.6B",
    "dimension": 512,
    "revision": "main",
    "query_prompt_name": "query"
  },
  "vector": {
    "backend": "sqlite-vec",
    "allow_python_fallback": true
  }
}
```

The embedding model is lazy-loaded. `sqlite-vec` and Sentence Transformers are optional dependencies; missing optional packages do not prevent normal WorkBot startup. When vector dependencies are absent, the canonical corpus and FTS5 channel still work. The Python exact-cosine fallback exists for migration/tests and small local use, not as the intended large-corpus backend.

Install the optional stack on Windows:

```powershell
cd D:\code\workbot
.\scripts\setup-rag.ps1 -DownloadModel
```

The script installs the `rag` extra, probes `sqlite-vec`, and optionally downloads/loads the default embedding model.

## Corpus and incremental indexing

RAG uses these derived tables:

- `rag_documents`: source identity, type, namespace, URI/path, metadata and content hash.
- `rag_chunks`: semantic retrieval units with section/symbol/line references.
- `rag_fts`: FTS5 lexical index.
- `rag_vector_indexes`: versioned embedding-space metadata.
- `rag_chunk_embeddings`: chunk hash + embedding index marker.
- `rag_vector_fallback`: dependency-free fallback vectors when sqlite-vec is unavailable.

The embedding model signature, revision and dimension are part of the vector-index identity. A model/dimension change creates/selects a different logical index rather than overwriting unrelated vectors. Chunk content hashes make embedding incremental: unchanged chunks are not recomputed.

The chunker itself is versioned (`semantic-v1`). A future chunking-policy change therefore invalidates the derived document hash and rebuilds affected chunks safely.

## Source-specific chunking

- **Memory**: one memory item is one retrieval document/chunk. Memory scope remains authoritative.
- **Markdown/text**: heading/paragraph-aware chunks preserve section context.
- **Python**: `class`/`def` boundaries are preferred.
- **C/C++ and similar code**: top-level functions/types are detected heuristically and retain symbols plus line references.
- **Large generic text**: bounded paragraph/newline chunks are used as a fallback.

Code chunks are *candidate locators*. For concrete implementation claims, Agent guidance still requires opening/reading the real source rather than treating a retrieved chunk as a complete proof.

## Hybrid ranking

V1.11.0 retrieves lexical and vector candidates separately, then uses weighted Reciprocal Rank Fusion:

```text
RRF(doc) = sum(channel_weight / (k + rank_channel(doc)))
```

Default values:

- lexical top-k: 30
- vector top-k: 30
- final top-k: 8
- `rrf_k`: 60
- lexical weight: 1.0
- vector weight: 1.2
- maximum chunks per document: 2

Exact query matches in title/path/section/symbol receive a small deterministic boost. Raw BM25 and cosine scores are deliberately not added together because their scales are unrelated.

## Optional reranker

A second-stage reranker is implemented but disabled by default:

```json
{
  "reranker": {
    "enabled": false,
    "provider": "sentence-transformers",
    "model": "Qwen/Qwen3-Reranker-0.6B",
    "revision": "main",
    "candidate_k": 16
  }
}
```

When enabled, it reranks a small RRF candidate set. Any reranker loading/runtime error falls back to the RRF order rather than breaking the answer path.

## Memory scope and provenance

Memory retrieval is **cross-conversation by default**. A project may span several WeLink groups and private chats, so relevant `conversation:<id>` memories from other conversations may enter the Hybrid RAG candidate set together with global memory. The original scope is preserved as provenance and is shown in source metadata; it is not an access wall in the default configuration.

To restore strict conversation-local retrieval, configure both:

```json
{
  "memory": {"cross_conversation_retrieval": false},
  "rag": {"retrieval": {"memory_scope": "conversation"}}
}
```

The sqlite-vec path and Python fallback apply the same configured scope rule before final top-k selection. Agent code is not allowed to query WorkBot's SQLite database directly for RAG; active retrieval is mediated by WorkBot.

## Passive and active RAG

### Passive RAG

Before a normal Agent turn, WorkBot retrieves a small mixed evidence set. If the RAG corpus has not yet been populated, the existing Memory/Workspace lexical paths remain the fallback so upgrading does not create a blind period.

### Active RAG

If evidence is insufficient, CodeAgent may return a hidden request such as:

```text
<WORKBOT_RAG>{"query":"ExecReScanStream parameter resend","sources":["memory","workspace","code"],"top_k":10}</WORKBOT_RAG>
```

WorkBot performs the scoped read-only lookup, injects the results into the **same** CodeAgent session, and lets reasoning continue. At most two rounds are enabled by default. The marker is internal and never sent to WeLink.

## Operator commands

```text
/rag status
/rag sync [status|stop]
/rag embed [N|all|status|stop]
/rag search stream pbe rescan
/rag reindex
```

- `/rag status`: corpus/dependency/vector/reranker state, coverage, current jobs and recent errors.
- `/rag sync`: start a background Memory + Workspace -> canonical RAG corpus synchronization. `/rag sync status|stop` inspects or cooperatively stops it. No embedding model is needed.
- `/rag embed [N]`: start a background job for up to `N` pending chunks; `/rag embed all` continues through all pending chunks, while `status|stop` controls the job.
- `/rag search <query>`: inspect Hybrid RAG results using the configured Memory scope.
- `/rag reindex`: clear the current embedding markers/vectors; corpus text remains and will be rebuilt incrementally.

Normal idle maintenance automatically performs sync and bounded embedding work when configured. Manual jobs and maintenance do not run competing sync/embed loops.

## Future sources

`RAGService.upsert_text_document()` provides a source-agnostic ingestion API for product manuals, Wiki pages, Web/W3 caches, mail summaries, connector documents, etc. Callers are responsible for source-specific authorization/access filtering before ingestion.

Useful source types include:

```text
manual  -> [产品手册: ...]
wiki    -> [Wiki: title | URL]
web/w3  -> [Web: title | URL]
code    -> [源码: path::symbol (lines)]
```

This aligns RAG evidence with WorkBot's existing evidence-grounded answer policy.

## Windows/Linux topology

The mutable RAG database remains owned by the Windows supervisor. Linux workers should consume RAG through WorkBot-controlled context/RPC rather than maintaining a second writable vector copy. This keeps access policy, versioning and SQLite writes centralized.

## Validation and rollout

First install the optional dependencies, then:

```powershell
cd D:\code\workbot
.\scripts\check-release.ps1 -RagRuntime
```

For initial corpus construction:

```text
/rag sync
/rag embed all
/rag status
/rag search stream pbe
```

`/rag embed all` runs in background micro-batches and commits each micro-batch incrementally; use `/rag embed status` or `/rag embed stop` at any time. Idle maintenance can also fill the corpus gradually. Model loading/download happens only when the embedding path is actually used.


## Runtime jobs and CPU guidance

Large CPU-only embedding runs are now background jobs rather than one blocking command handler. Use:

```text
/rag embed 5000
/rag embed all
/rag embed status
/rag embed stop
/rag sync
/rag sync status
/rag sync stop
```

`rag.index.job_batch_chunks` (default 32) is the outer commit/progress micro-batch. `rag.embedding.batch_size` (default 16) remains the Sentence Transformers model batch. Each completed micro-batch is persisted immediately, so stopping/restarting loses at most the current model call rather than a multi-thousand-chunk run.

WorkBot intentionally uses one embedding worker. When CPU is already saturated, increasing Python concurrency usually adds contention instead of throughput. `rag.embedding.cpu_threads=0` leaves PyTorch thread selection automatic; a lower positive value can reserve interactive CPU headroom.

Pending embeddings are prioritized approximately as Memory, product manuals, Wiki, Web, code, then generic Workspace. `/rag status` reports embedded/pending counts, coverage, current sync/embed job state, device/model batch/CPU thread configuration and errors.

Memory Hybrid RAG defaults to `rag.retrieval.memory_scope=all`, allowing relevant memories from other WeLink conversations to be recalled. Set it to `conversation` to restore V1.11.0 vector/FTS isolation.
