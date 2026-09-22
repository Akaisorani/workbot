from __future__ import annotations

import hashlib
import math
import re
import logging
import time
from dataclasses import dataclass
from typing import Protocol, Sequence


log = logging.getLogger(__name__)


class EmbeddingUnavailable(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    @property
    def dimension(self) -> int: ...

    @property
    def signature(self) -> str: ...

    def embed_query(self, text: str) -> list[float]: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


def _normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in values))
    if norm <= 0:
        return [0.0 for _ in values]
    return [float(x) / norm for x in values]


@dataclass(slots=True)
class HashEmbeddingProvider:
    """Small deterministic embedding provider used for tests/offline diagnostics.

    This is intentionally not the production default.  It gives the RAG layer a
    dependency-free way to exercise indexing, vector persistence and hybrid
    ranking before a SentenceTransformer model is installed.
    """

    dim: int = 64

    @property
    def dimension(self) -> int:
        return int(self.dim)

    @property
    def signature(self) -> str:
        return f"hash-v1:{self.dimension}"

    @staticmethod
    def _tokens(text: str) -> list[str]:
        # Keep identifiers/path fragments intact while also emitting CJK chars so
        # Chinese semantic-ish overlap can be tested without an external model.
        words = re.findall(r"[A-Za-z0-9_./\\:-]+|[\u3400-\u9fff]", text or "")
        return [w.lower() for w in words if w.strip()]

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimension
        for token in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
            idx = int.from_bytes(digest[:8], "little") % self.dimension
            sign = 1.0 if digest[8] & 1 else -1.0
            vec[idx] += sign
        return _normalize(vec)

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(x) for x in texts]


class SentenceTransformerEmbeddingProvider:
    """Lazy SentenceTransformers-backed provider for Qwen3 embeddings.

    Qwen3-Embedding-0.6B returns a 1024-d vector by default and supports
    Matryoshka truncation.  We deliberately slice to the configured dimension
    and re-normalize after slicing so the stored cosine space is consistent.
    """

    def __init__(self, cfg: dict | None = None):
        cfg = dict(cfg or {})
        self.model_name = str(cfg.get("model") or "Qwen/Qwen3-Embedding-0.6B")
        self._dimension = int(cfg.get("dimension", 512))
        self.revision = str(cfg.get("revision") or "main")
        self.device = str(cfg.get("device") or "auto")
        self.batch_size = max(1, int(cfg.get("batch_size", 16)))
        self.query_prompt_name = str(cfg.get("query_prompt_name") or "query")
        self.cpu_threads = max(0, int(cfg.get("cpu_threads", 0) or 0))
        self._model = None

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def signature(self) -> str:
        return f"sentence-transformers:{self.model_name}@{self.revision}:{self.dimension}"

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:  # pragma: no cover - depends on optional dep
            raise EmbeddingUnavailable(
                "sentence-transformers is not installed; install WorkBot with the 'rag' extra"
            ) from exc
        kwargs = {}
        if self.cpu_threads > 0:
            try:
                import torch
                torch.set_num_threads(self.cpu_threads)
                # Inter-op is process-global and may already be initialized.
                try:
                    torch.set_num_interop_threads(max(1, min(4, self.cpu_threads)))
                except RuntimeError:
                    pass
            except Exception:
                log.warning("Unable to apply RAG embedding cpu_threads=%s", self.cpu_threads)
        if self.device and self.device.lower() != "auto":
            kwargs["device"] = self.device
        try:
            self._model = SentenceTransformer(self.model_name, revision=self.revision, **kwargs)
        except Exception as exc:  # pragma: no cover - model download/runtime
            raise EmbeddingUnavailable(f"failed to load embedding model {self.model_name}: {exc}") from exc
        return self._model

    def _truncate(self, row) -> list[float]:
        try:
            values = row.tolist()
        except AttributeError:
            values = list(row)
        if len(values) < self.dimension:
            raise EmbeddingUnavailable(
                f"embedding model returned {len(values)} dimensions, below configured {self.dimension}"
            )
        return _normalize(values[: self.dimension])

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        started = time.monotonic()
        log.info("RAG embedding query start model=%s mode=query prompt=%s dimension=%d device=%s", self.model_name, self.query_prompt_name or "none", self.dimension, self.device)
        kwargs = {"convert_to_numpy": True, "show_progress_bar": False}
        # Qwen's model card recommends its built-in `query` prompt.  Older/custom
        # SentenceTransformer packages may not expose that prompt; fall back to
        # ordinary encode instead of breaking WorkBot retrieval.
        try:
            row = model.encode([text], prompt_name=self.query_prompt_name, **kwargs)[0]
        except (KeyError, ValueError, TypeError):
            row = model.encode([text], **kwargs)[0]
        out = self._truncate(row)
        log.info("RAG embedding query done elapsed=%.3fs", time.monotonic() - started)
        return out

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._load()
        started = time.monotonic()
        log.info("RAG embedding documents start model=%s mode=document prompt=none count=%d model_batch=%d dimension=%d device=%s cpu_threads=%d", self.model_name, len(texts), self.batch_size, self.dimension, self.device, self.cpu_threads)
        rows = model.encode(
            list(texts),
            batch_size=self.batch_size,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        out = [self._truncate(row) for row in rows]
        elapsed = time.monotonic() - started
        log.info("RAG embedding documents done count=%d elapsed=%.3fs rate=%.3f chunks/s", len(out), elapsed, (len(out)/elapsed if elapsed > 0 else 0.0))
        return out


def build_embedding_provider(cfg: dict | None = None) -> EmbeddingProvider:
    cfg = dict(cfg or {})
    provider = str(cfg.get("provider") or "sentence-transformers").strip().lower()
    if provider in {"hash", "test-hash", "diagnostic"}:
        return HashEmbeddingProvider(int(cfg.get("dimension", 64)))
    if provider in {"sentence-transformers", "sentence_transformers", "st", "qwen3"}:
        return SentenceTransformerEmbeddingProvider(cfg)
    raise ValueError(f"unsupported RAG embedding provider: {provider}")
