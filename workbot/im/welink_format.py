from __future__ import annotations

"""WeLink outbound text formatting helpers.

WorkBot's reasoning agents may answer with normal Markdown, while the WeLink PC
client uses a private inline representation for native code blocks.  This module
keeps that transport quirk outside the agent prompt and provides a deterministic
formatter/splitter that can be unit-tested without invoking ``welink-cli``.

Observed WeLink history representation for a native code block::

    <START><code>\n{"lang":"python","lineBreak":false,"totalLines":3}<END>

The start/end sentinels below come from messages created by the WeLink desktop
client and then read back through ``query-history-message``.
"""

from dataclasses import dataclass
import json
import re
import unicodedata

# WeLink desktop's hidden native-code sentinels, recovered from history output.
WELINK_CODE_START = "\u2009\u2009\u2009\u200b\u200b\u200b"
WELINK_CODE_END = "\u2009\u2009\u200b\u200b"

SUPPORTED_CODE_LANGUAGES = {
    "c", "cpp", "css", "go", "html", "java", "javascript", "python",
    "rust", "sql", "typescript", "xml",
}

_LANGUAGE_ALIASES = {
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "py": "python",
    "js": "javascript",
    "jsx": "javascript",
    "node": "javascript",
    "json": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "rs": "rust",
    "golang": "go",
    # WeLink has no shell/PowerShell/plain-text syntax.  Python is used only as
    # a monospaced fallback; the code body itself is never changed.
    "bash": "python",
    "sh": "python",
    "shell": "python",
    "zsh": "python",
    "powershell": "python",
    "pwsh": "python",
    "ps1": "python",
    "cmd": "python",
    "bat": "python",
    "console": "python",
    "text": "python",
    "txt": "python",
    "plaintext": "python",
    "markdown": "python",
    "md": "python",
    "yaml": "python",
    "yml": "python",
    "toml": "python",
    "ini": "python",
    "diff": "python",
    "workbot-table": "python",
    "workbot-diagram": "python",
}

_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([^\s`]*)[^\n]*$", re.MULTILINE)
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


@dataclass(frozen=True)
class MarkdownSegment:
    kind: str  # "text" or "code"
    text: str = ""
    language: str = ""
    code: str = ""


def normalize_language(language: str) -> str:
    value = str(language or "").strip().lower()
    value = _LANGUAGE_ALIASES.get(value, value)
    return value if value in SUPPORTED_CODE_LANGUAGES else "python"


def encode_welink_code_block(code: str, language: str = "") -> str:
    """Encode one code block using the format emitted by the WeLink client."""
    body = str(code or "")
    # Markdown fences conventionally carry one delimiter newline that is not
    # part of the code body.  Preserve internal/trailing content but avoid an
    # extra blank line immediately before WeLink metadata.
    body = body[:-1] if body.endswith("\n") else body
    total_lines = 0 if not body else body.count("\n") + 1
    meta = json.dumps(
        {
            "lang": normalize_language(language),
            "lineBreak": False,
            "totalLines": total_lines,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"{WELINK_CODE_START}{body}\n{meta}{WELINK_CODE_END}"


def _parse_fenced_segments(text: str) -> list[MarkdownSegment]:
    """Parse normal Markdown fenced blocks while leaving malformed fences text."""
    value = str(text or "")
    out: list[MarkdownSegment] = []
    pos = 0
    n = len(value)
    while pos < n:
        m = _FENCE_OPEN_RE.search(value, pos)
        if not m:
            if pos < n:
                out.append(MarkdownSegment("text", text=value[pos:]))
            break
        if m.start() > pos:
            out.append(MarkdownSegment("text", text=value[pos:m.start()]))

        fence = m.group(1)
        marker = fence[0]
        min_len = len(fence)
        language = m.group(2) or ""
        body_start = m.end()
        if body_start < n and value[body_start] == "\n":
            body_start += 1

        close_re = re.compile(rf"^[ \t]{{0,3}}{re.escape(marker)}{{{min_len},}}[ \t]*$", re.MULTILINE)
        close = close_re.search(value, body_start)
        if not close:
            # Treat an unmatched opening fence as ordinary text.  This is safer
            # than hiding the tail of an answer in a half-created native block.
            out.append(MarkdownSegment("text", text=value[m.start():]))
            break

        code = value[body_start:close.start()]
        out.append(MarkdownSegment("code", language=language, code=code))
        # Keep the exact source whitespace after the closing fence.  Do not
        # synthesize an extra newline here: the next parser iteration will
        # naturally treat the original newline(s) as ordinary text.
        pos = close.end()
    if not out and value == "":
        return [MarkdownSegment("text", text="")]
    return out


def _split_markdown_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    # Basic escaped-pipe handling is sufficient for chat tables and avoids a
    # full Markdown parser dependency.
    cells: list[str] = []
    cur: list[str] = []
    escaped = False
    for ch in s:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
            cur.append(ch)
        elif ch == "|":
            cells.append("".join(cur).strip().replace("\\|", "|"))
            cur = []
        else:
            cur.append(ch)
    cells.append("".join(cur).strip().replace("\\|", "|"))
    return cells


def _display_width(text: str) -> int:
    width = 0
    for ch in str(text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
    return width


def _pad_display(text: str, width: int) -> str:
    value = str(text)
    return value + " " * max(0, width - _display_width(value))


def _render_markdown_table(lines: list[str]) -> str:
    rows = [_split_markdown_row(line) for i, line in enumerate(lines) if i != 1]
    if not rows:
        return ""
    cols = max(len(row) for row in rows)
    rows = [row + [""] * (cols - len(row)) for row in rows]
    widths = [max(_display_width(row[c]) for row in rows) for c in range(cols)]

    def border(left: str, mid: str, right: str) -> str:
        return left + mid.join("─" * (w + 2) for w in widths) + right

    rendered = [border("┌", "┬", "┐")]
    for r_i, row in enumerate(rows):
        rendered.append("│ " + " │ ".join(_pad_display(row[c], widths[c]) for c in range(cols)) + " │")
        if r_i == 0 and len(rows) > 1:
            rendered.append(border("├", "┼", "┤"))
    rendered.append(border("└", "┴", "┘"))
    return "\n".join(rendered)


def _convert_markdown_tables_in_text(text: str) -> str:
    lines = str(text or "").splitlines(keepends=True)
    if len(lines) < 2:
        return str(text or "")
    out: list[str] = []
    i = 0
    while i < len(lines):
        first = lines[i].rstrip("\r\n")
        second = lines[i + 1].rstrip("\r\n") if i + 1 < len(lines) else ""
        if "|" in first and _TABLE_SEPARATOR_RE.match(second):
            block = [first, second]
            j = i + 2
            while j < len(lines):
                candidate = lines[j].rstrip("\r\n")
                if not candidate.strip() or "|" not in candidate:
                    break
                block.append(candidate)
                j += 1
            table = _render_markdown_table(block)
            out.append(f"```workbot-table\n{table}\n```\n")
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "".join(out)


def normalize_markdown_for_welink(text: str) -> str:
    """Convert Markdown tables outside fences into monospaced table blocks."""
    segments = _parse_fenced_segments(str(text or ""))
    pieces: list[str] = []
    for seg in segments:
        if seg.kind == "text":
            pieces.append(_convert_markdown_tables_in_text(seg.text))
        else:
            language = seg.language or ""
            # The parser leaves the original post-fence whitespace in the next
            # text segment.  Re-adding a newline here would duplicate it.
            pieces.append(f"```{language}\n{seg.code}```")
    return "".join(pieces).rstrip("\n")


def render_welink_markdown(text: str) -> str:
    """Render Markdown fences/tables into WeLink's native inline code format."""
    normalized = normalize_markdown_for_welink(str(text or ""))
    segments = _parse_fenced_segments(normalized)
    out: list[str] = []
    previous_was_code = False
    for seg in segments:
        if seg.kind == "code":
            out.append(encode_welink_code_block(seg.code, seg.language))
            previous_was_code = True
            continue

        value = seg.text
        if previous_was_code:
            # WeLink's native block already has clear visual separation.  Chat
            # models often emit two or more blank lines after a Markdown fence;
            # preserving all of them makes the native block look as if WorkBot
            # inserted several empty rows.  Keep at most the one line break
            # needed to continue with following prose, while never touching
            # whitespace inside the code block itself.  Whitespace-only blank
            # lines are compacted too.
            leading_breaks = re.match(r"^(?:[ \t]*\r?\n)+", value)
            if leading_breaks:
                rest = value[leading_breaks.end():]
                value = ("\n" + rest) if rest else ""
        out.append(value)
        if value:
            previous_was_code = False
    return "".join(out)


def count_code_blocks(text: str) -> int:
    normalized = normalize_markdown_for_welink(str(text or ""))
    return sum(1 for seg in _parse_fenced_segments(normalized) if seg.kind == "code")


def _render_len(text: str) -> int:
    return len(render_welink_markdown(text))


def _preferred_text_cut(text: str, limit: int) -> int:
    """Choose a human-friendly split point at or before ``limit``."""
    if len(text) <= limit:
        return len(text)
    window = text[: limit + 1]
    # Prefer paragraph, then line, sentence punctuation, then whitespace.
    for needle in ("\n\n", "\n"):
        idx = window.rfind(needle)
        if idx >= max(1, limit // 3):
            return idx + len(needle)
    punctuation = "。！？；.!?;"
    for i in range(len(window) - 1, max(0, limit // 3), -1):
        if window[i - 1] in punctuation:
            return i
    for i in range(len(window) - 1, max(0, limit // 3), -1):
        if window[i - 1].isspace():
            return i
    return max(1, limit)


def _split_plain_text(text: str, max_chars: int) -> list[str]:
    remaining = str(text or "")
    out: list[str] = []
    while remaining:
        if _render_len(remaining) <= max_chars:
            out.append(remaining)
            break
        # Plain text has no render expansion, so max_chars is the character cap.
        cut = _preferred_text_cut(remaining, max_chars)
        out.append(remaining[:cut])
        remaining = remaining[cut:]
    return [x for x in out if x != ""]


def _code_markdown(language: str, code: str) -> str:
    body = str(code or "")
    if body and not body.endswith("\n"):
        body += "\n"
    return f"```{language}\n{body}```"


def _split_code_segment(seg: MarkdownSegment, max_chars: int) -> list[str]:
    whole = _code_markdown(seg.language, seg.code)
    if _render_len(whole) <= max_chars:
        return [whole]

    # Split at line boundaries.  A pathological single line is hard-split only
    # when necessary; every resulting part is still a valid independent block.
    lines = seg.code.splitlines(keepends=True)
    if not lines and seg.code:
        lines = [seg.code]
    parts: list[str] = []
    current = ""
    for line in lines:
        candidate = current + line
        if current and _render_len(_code_markdown(seg.language, candidate)) > max_chars:
            parts.append(_code_markdown(seg.language, current))
            current = ""
        if _render_len(_code_markdown(seg.language, line)) <= max_chars:
            current += line
            continue

        # One source line itself is too large.  Determine a safe payload budget
        # iteratively because native metadata length also depends on line count.
        rest = line
        while rest:
            lo, hi = 1, len(rest)
            best = 1
            while lo <= hi:
                mid = (lo + hi) // 2
                if _render_len(_code_markdown(seg.language, rest[:mid])) <= max_chars:
                    best = mid
                    lo = mid + 1
                else:
                    hi = mid - 1
            parts.append(_code_markdown(seg.language, rest[:best]))
            rest = rest[best:]
    if current:
        parts.append(_code_markdown(seg.language, current))
    return parts


def split_welink_markdown(text: str, max_chars: int = 3500) -> list[str]:
    """Split one logical reply into WeLink-safe Markdown messages.

    Guarantees for every returned message after ``render_welink_markdown``:
      * rendered length is at most ``max_chars``;
      * it contains at most one native code block;
      * code fences remain syntactically complete;
      * source order is preserved.
    """
    max_chars = max(256, int(max_chars))
    normalized = normalize_markdown_for_welink(str(text or ""))
    segments = _parse_fenced_segments(normalized)
    if not segments:
        return [""]

    atomic: list[MarkdownSegment] = []
    for seg in segments:
        if seg.kind == "code":
            for part in _split_code_segment(seg, max_chars):
                parsed = _parse_fenced_segments(part)
                code_seg = next((x for x in parsed if x.kind == "code"), None)
                if code_seg is not None:
                    atomic.append(code_seg)
        else:
            # Text will be split again while packing, but making clearly-safe
            # pieces here avoids large intermediate strings.
            for piece in _split_plain_text(seg.text, max_chars) or ([""] if seg.text == "" else []):
                atomic.append(MarkdownSegment("text", text=piece))

    messages: list[str] = []
    current = ""
    current_code_blocks = 0

    def flush() -> None:
        nonlocal current, current_code_blocks
        value = current
        if value or not messages:
            messages.append(value)
        current = ""
        current_code_blocks = 0

    for seg in atomic:
        if seg.kind == "code":
            block = _code_markdown(seg.language, seg.code)
            if current_code_blocks >= 1:
                flush()
            candidate = block if not current else current.rstrip("\n") + "\n" + block
            if current and _render_len(candidate) > max_chars:
                flush()
                candidate = block
            current = candidate
            current_code_blocks = 1
            continue

        remaining = seg.text
        while remaining:
            separator = "" if not current or current.endswith("\n") or remaining.startswith("\n") else ""
            candidate = current + separator + remaining
            if _render_len(candidate) <= max_chars:
                current = candidate
                remaining = ""
                break
            if current:
                # Fill remaining capacity using a semantic text cut.
                available = max_chars - _render_len(current)
                if available > 0:
                    cut = _preferred_text_cut(remaining, available)
                    piece = remaining[:cut]
                    if piece and _render_len(current + piece) <= max_chars:
                        current += piece
                        remaining = remaining[cut:]
                flush()
            else:
                cut = _preferred_text_cut(remaining, max_chars)
                current = remaining[:cut]
                remaining = remaining[cut:]
                flush()

    if current or not messages:
        flush()

    # Defensive final validation.  If a future formatter change increases wire
    # overhead, fail loudly in tests/development instead of silently truncating.
    for message in messages:
        if count_code_blocks(message) > 1:
            raise AssertionError("WeLink split produced more than one code block")
        if _render_len(message) > max_chars:
            raise AssertionError("WeLink split produced an oversized message")
    return messages
