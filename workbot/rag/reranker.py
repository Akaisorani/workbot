from __future__ import annotations

from typing import Protocol, Sequence


class RerankerUnavailable(RuntimeError):
    pass


class Reranker(Protocol):
    @property
    def signature(self) -> str: ...

    def score(self, query: str, passages: Sequence[str]) -> list[float]: ...


class SentenceTransformerCrossEncoderReranker:
    def __init__(self, cfg: dict | None = None):
        cfg = dict(cfg or {})
        self.model_name = str(cfg.get("model") or "Qwen/Qwen3-Reranker-0.6B")
        self.revision = str(cfg.get("revision") or "main")
        self.device = str(cfg.get("device") or "auto")
        self.batch_size = max(1, int(cfg.get("batch_size", 8)))
        self._model = None

    @property
    def signature(self) -> str:
        return f"cross-encoder:{self.model_name}@{self.revision}"

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import CrossEncoder
        except Exception as exc:  # pragma: no cover - optional dependency
            raise RerankerUnavailable("sentence-transformers CrossEncoder is unavailable") from exc
        kwargs = {"revision": self.revision}
        if self.device and self.device.lower() != "auto":
            kwargs["device"] = self.device
        try:
            self._model = CrossEncoder(self.model_name, **kwargs)
        except Exception as exc:  # pragma: no cover - model/runtime dependent
            raise RerankerUnavailable(f"failed to load reranker {self.model_name}: {exc}") from exc
        return self._model

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        if not passages:
            return []
        model = self._load()
        values = model.predict(
            [(query, passage) for passage in passages],
            batch_size=self.batch_size,
            show_progress_bar=False,
        )
        try:
            return [float(x) for x in values.tolist()]
        except AttributeError:
            return [float(x) for x in values]


def build_reranker(cfg: dict | None = None):
    cfg = dict(cfg or {})
    if not bool(cfg.get("enabled", False)):
        return None
    provider = str(cfg.get("provider") or "sentence-transformers").lower()
    if provider in {"sentence-transformers", "sentence_transformers", "cross-encoder", "qwen3"}:
        return SentenceTransformerCrossEncoderReranker(cfg)
    raise ValueError(f"unsupported RAG reranker provider: {provider}")
