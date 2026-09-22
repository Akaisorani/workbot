from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from workbot.conversation.models import IncomingMessage

log = logging.getLogger(__name__)

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


class ImageContextProcessor:
    """Best-effort local image text extraction for WeLink messages.

    Image understanding is deliberately an optional enrichment layer. Missing
    files, an unavailable OCR dependency, or OCR failures never reject the
    underlying chat message. The normal text/quote path remains authoritative.
    """

    def __init__(self, cfg: dict | None = None, *, self_accounts: list[str] | None = None):
        cfg = dict(cfg or {})
        image_cfg = dict(cfg.get("image", cfg) or {})
        ocr_cfg = dict(image_cfg.get("ocr", {}) or {})
        self.enabled = bool(image_cfg.get("enabled", True))
        self.ocr_enabled = bool(ocr_cfg.get("enabled", True))
        self.provider = str(ocr_cfg.get("provider", "rapidocr") or "rapidocr").strip().lower()
        self.timeout_seconds = max(1.0, float(ocr_cfg.get("timeout_seconds", 20)))
        self.path_wait_seconds = max(0.0, float(image_cfg.get("path_wait_seconds", 2.0)))
        self.max_images = max(1, int(image_cfg.get("max_images_per_message", 4)))
        self.max_chars = max(200, int(ocr_cfg.get("max_chars_per_image", 5000)))
        self.max_file_mb = max(1.0, float(image_cfg.get("max_file_mb", 30)))
        self._engine: Any = None
        self._engine_lock = threading.Lock()
        self._ocr_call_lock = threading.Lock()
        self._semaphore = asyncio.Semaphore(max(1, int(ocr_cfg.get("max_concurrent", 1))))
        self._allowed_roots = self._build_allowed_roots(image_cfg, self_accounts or [])

    @staticmethod
    def _build_allowed_roots(image_cfg: dict, self_accounts: list[str]) -> tuple[Path, ...]:
        roots: list[Path] = []
        for raw in image_cfg.get("allowed_roots", []) or []:
            if str(raw).strip():
                roots.append(Path(os.path.expandvars(os.path.expanduser(str(raw)))))
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            for account in self_accounts:
                roots.append(Path(appdata) / "WeLink_Desktop" / "appdata" / "IM" / str(account) / "ReceiveFiles")
        # De-duplicate while keeping order. Resolve only existing roots because
        # Windows drive syntax is not meaningful on non-Windows test hosts.
        seen: set[str] = set()
        out: list[Path] = []
        for root in roots:
            key = os.path.normcase(os.path.normpath(str(root)))
            if key in seen:
                continue
            seen.add(key)
            out.append(root)
        return tuple(out)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ocr_enabled": self.ocr_enabled,
            "provider": self.provider,
            "allowed_roots": [str(x) for x in self._allowed_roots],
            "engine_loaded": self._engine is not None,
        }

    def _is_allowed_path(self, path: Path) -> bool:
        if not self._allowed_roots:
            # No auto/configured roots normally means the deployment is not on
            # a WeLink desktop host. Do not read arbitrary paths supplied by a
            # message payload in that case.
            return False
        candidate = os.path.normcase(os.path.abspath(str(path)))
        for root in self._allowed_roots:
            base = os.path.normcase(os.path.abspath(str(root)))
            try:
                if os.path.commonpath([candidate, base]) == base:
                    return True
            except ValueError:
                continue
        return False

    def _wait_for_path(self, raw: str) -> Path | None:
        path = Path(raw)
        deadline = time.monotonic() + self.path_wait_seconds
        while True:
            if path.exists() and path.is_file():
                return path
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.15)

    def _rapidocr_engine(self):
        if self._engine is not None:
            return self._engine
        with self._engine_lock:
            if self._engine is None:
                from rapidocr import RapidOCR  # optional dependency
                self._engine = RapidOCR()
        return self._engine

    @staticmethod
    def _rapidocr_text(result: Any) -> tuple[str, float | None]:
        # RapidOCR >=3 returns RapidOCROutput with txts/scores. Older releases
        # returned (rows, elapse), where rows contained [box, text, score].
        if result is None:
            return "", None
        if hasattr(result, "txts"):
            txts = list(getattr(result, "txts") or [])
            scores = list(getattr(result, "scores", ()) or ())
            text = "\n".join(str(x).strip() for x in txts if str(x).strip())
            avg = (sum(float(x) for x in scores) / len(scores)) if scores else None
            return text, avg
        rows = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        if isinstance(rows, (list, tuple)):
            texts: list[str] = []
            scores: list[float] = []
            for row in rows:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    value = str(row[1]).strip()
                    if value:
                        texts.append(value)
                    if len(row) >= 3:
                        try:
                            scores.append(float(row[2]))
                        except Exception:
                            pass
            avg = (sum(scores) / len(scores)) if scores else None
            return "\n".join(texts), avg
        return "", None

    def _ocr_path(self, path: Path) -> tuple[str, float | None]:
        engine = self._rapidocr_engine()
        try:
            result = engine(str(path))
            text, score = self._rapidocr_text(result)
            if text or path.suffix.lower() != ".gif":
                return text, score
        except Exception:
            if path.suffix.lower() != ".gif":
                raise
        # OpenCV-based OCR readers do not consistently decode GIF. When Pillow
        # is available, extract the first frame into a temporary PNG and retry.
        from PIL import Image
        with Image.open(path) as image:
            image.seek(0)
            frame = image.convert("RGB")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                temp_path = Path(tmp.name)
            try:
                frame.save(temp_path, format="PNG")
                return self._rapidocr_text(engine(str(temp_path)))
            finally:
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _enrich_one_sync(self, raw_path: str) -> dict[str, Any]:
        info: dict[str, Any] = {"path": str(raw_path), "status": "unavailable", "text": ""}
        suffix = Path(raw_path).suffix.lower()
        if suffix not in _IMAGE_SUFFIXES:
            info["reason"] = "unsupported image type"
            return info
        if not self._is_allowed_path(Path(raw_path)):
            info["reason"] = "path outside allowed WeLink image roots"
            return info
        path = self._wait_for_path(raw_path)
        if path is None:
            info["reason"] = "local image file not found"
            return info
        info["path"] = str(path)
        # A trusted local image path is already useful to a multimodal CodeAgent,
        # even if OCR is disabled/unavailable. OCR is only a text supplement.
        info["status"] = "available"
        try:
            size_mb = path.stat().st_size / (1024 * 1024)
            if size_mb > self.max_file_mb:
                info["reason"] = f"image too large ({size_mb:.1f} MB)"
                return info
        except OSError:
            pass
        if not self.ocr_enabled or self.provider in {"none", "disabled"}:
            info["status"] = "available"
            info["reason"] = "OCR disabled"
            return info
        if self.provider != "rapidocr":
            info["reason"] = f"unsupported OCR provider: {self.provider}"
            return info
        try:
            with self._ocr_call_lock:
                text, score = self._ocr_path(path)
            info["status"] = "ok" if text.strip() else "empty"
            info["text"] = text.strip()[: self.max_chars]
            if score is not None:
                info["confidence"] = round(float(score), 4)
            if not text.strip():
                info["reason"] = "OCR returned no text"
            return info
        except ModuleNotFoundError as exc:
            info["status"] = "available"
            info["reason"] = f"OCR dependency unavailable: {exc.name or exc}"
            return info
        except Exception as exc:
            info["status"] = "available"
            info["reason"] = f"OCR failed: {type(exc).__name__}: {exc}"[:500]
            return info

    async def _enrich_paths(self, paths: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
        if not self.enabled or not paths:
            return ()
        results: list[dict[str, Any]] = []
        for raw in paths[: self.max_images]:
            async with self._semaphore:
                try:
                    item = await asyncio.wait_for(
                        asyncio.to_thread(self._enrich_one_sync, raw),
                        timeout=self.timeout_seconds + self.path_wait_seconds + 1.0,
                    )
                except asyncio.TimeoutError:
                    item = {"path": str(raw), "status": "unavailable", "text": "", "reason": "OCR timeout"}
                results.append(item)
        return tuple(results)

    async def enrich(self, msg: IncomingMessage) -> IncomingMessage:
        """Return the same message enriched with current and quoted image OCR."""
        if not self.enabled:
            return msg
        from dataclasses import replace

        current = await self._enrich_paths(tuple(msg.image_paths or ()))
        quote = dict(msg.quote or {}) if msg.quote else None
        if quote:
            qpaths = tuple(str(x) for x in (quote.get("image_paths") or []) if str(x))
            if qpaths:
                quote["image_context"] = list(await self._enrich_paths(qpaths))
        return replace(msg, image_context=current, quote=quote)
