from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_CODE_EXTS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".py", ".go", ".rs", ".java", ".js", ".ts", ".sql"}
_MARKDOWN_EXTS = {".md", ".markdown", ".rst"}
CHUNKER_VERSION = "semantic-v1"


@dataclass(slots=True)
class ChunkSpec:
    content: str
    section: str = ""
    symbol: str = ""
    start_ref: str = ""
    end_ref: str = ""


def _bounded_parts(text: str, *, max_chars: int = 4500, min_split_chars: int = 1800) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    out: list[str] = []
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + max_chars)
        if end < len(text):
            cut = text.rfind("\n\n", pos + min_split_chars, end)
            if cut < 0:
                cut = text.rfind("\n", pos + min_split_chars, end)
            if cut >= 0:
                end = cut
        piece = text[pos:end].strip()
        if piece:
            out.append(piece)
        pos = max(end, pos + 1)
    return out


def _markdown_chunks(text: str, *, max_chars: int) -> list[ChunkSpec]:
    lines = (text or "").splitlines()
    sections: list[tuple[str, list[str], int]] = []
    headings: list[str] = []
    buf: list[str] = []
    start_line = 1

    def flush() -> None:
        nonlocal buf, start_line
        body = "\n".join(buf).strip()
        if body:
            sections.append((" > ".join(headings), list(buf), start_line))
        buf = []

    for idx, line in enumerate(lines, start=1):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if m:
            flush()
            level = len(m.group(1))
            headings[:] = headings[: level - 1]
            headings.append(m.group(2).strip())
            start_line = idx
            buf = [line]
        else:
            if not buf:
                start_line = idx
            buf.append(line)
    flush()

    out: list[ChunkSpec] = []
    for section, section_lines, line0 in sections or [("", lines, 1)]:
        body = "\n".join(section_lines).strip()
        cursor = line0
        for part in _bounded_parts(body, max_chars=max_chars):
            line_count = part.count("\n") + 1
            out.append(ChunkSpec(
                content=part,
                section=section,
                start_ref=f"L{cursor}",
                end_ref=f"L{cursor + line_count - 1}",
            ))
            cursor += line_count
    return out


def _python_symbol_chunks(text: str, *, max_chars: int) -> list[ChunkSpec]:
    lines = (text or "").splitlines()
    starts: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = re.match(r"^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b", line)
        if m:
            starts.append((i, m.group(1)))
    if not starts:
        return []
    starts.append((len(lines), ""))
    out: list[ChunkSpec] = []
    if starts[0][0] > 0:
        prefix = "\n".join(lines[: starts[0][0]]).strip()
        for part in _bounded_parts(prefix, max_chars=max_chars):
            out.append(ChunkSpec(content=part, start_ref="L1", end_ref=f"L{starts[0][0]}"))
    for pos in range(len(starts) - 1):
        start, symbol = starts[pos]
        end = starts[pos + 1][0]
        block = "\n".join(lines[start:end]).strip()
        cursor = start + 1
        for part in _bounded_parts(block, max_chars=max_chars):
            n = part.count("\n") + 1
            out.append(ChunkSpec(content=part, symbol=symbol, start_ref=f"L{cursor}", end_ref=f"L{cursor+n-1}"))
            cursor += n
    return out


def _c_like_symbol_chunks(text: str, *, max_chars: int) -> list[ChunkSpec]:
    lines = (text or "").splitlines()
    # Heuristic only: locate likely top-level function/type starts and then use
    # the next top-level-looking start as a boundary. Exact code reading remains
    # the final authority after RAG locates a candidate.
    start_re = re.compile(
        r"^\s*(?:typedef\s+)?(?:struct|class|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b|"
        r"^\s*(?!if\b|for\b|while\b|switch\b|catch\b)(?:[A-Za-z_][\w:<>,*&\s]+\s+)?([A-Za-z_~][\w:~]*)\s*\([^;]*\)\s*(?:const\s*)?\{\s*$"
    )
    starts: list[tuple[int, str]] = []
    brace_depth = 0
    for i, line in enumerate(lines):
        if brace_depth == 0:
            m = start_re.match(line)
            if m:
                starts.append((i, (m.group(1) or m.group(2) or "").strip()))
        # Strip strings/comments only approximately; this is a chunking hint,
        # not a parser and never substitutes for source inspection.
        brace_depth += line.count("{") - line.count("}")
        brace_depth = max(0, brace_depth)
    if not starts:
        return []
    starts.append((len(lines), ""))
    out: list[ChunkSpec] = []
    if starts[0][0] > 0:
        prefix = "\n".join(lines[: starts[0][0]]).strip()
        for part in _bounded_parts(prefix, max_chars=max_chars):
            out.append(ChunkSpec(content=part, start_ref="L1", end_ref=f"L{starts[0][0]}"))
    for pos in range(len(starts) - 1):
        start, symbol = starts[pos]
        end = starts[pos + 1][0]
        # Include a few immediately preceding comment lines when possible.
        comment_start = start
        while comment_start > 0 and start - comment_start < 5:
            prev = lines[comment_start - 1].strip()
            if prev.startswith(("//", "/*", "*", "*/")) or not prev:
                comment_start -= 1
            else:
                break
        block = "\n".join(lines[comment_start:end]).strip()
        cursor = comment_start + 1
        for part in _bounded_parts(block, max_chars=max_chars):
            n = part.count("\n") + 1
            out.append(ChunkSpec(content=part, symbol=symbol, start_ref=f"L{cursor}", end_ref=f"L{cursor+n-1}"))
            cursor += n
    return out


def chunk_document(text: str, path: str, *, max_chars: int = 4500) -> list[ChunkSpec]:
    suffix = Path(path).suffix.lower()
    if suffix in _MARKDOWN_EXTS:
        chunks = _markdown_chunks(text, max_chars=max_chars)
    elif suffix == ".py":
        chunks = _python_symbol_chunks(text, max_chars=max_chars)
    elif suffix in _CODE_EXTS:
        chunks = _c_like_symbol_chunks(text, max_chars=max_chars)
    else:
        chunks = []
    if chunks:
        return chunks
    return [ChunkSpec(content=p) for p in _bounded_parts(text, max_chars=max_chars)]


def is_code_path(path: str) -> bool:
    return Path(path).suffix.lower() in _CODE_EXTS
