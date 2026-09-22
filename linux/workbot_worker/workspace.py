from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable

_DEFAULT_MARKERS = (
    '.git', 'pyproject.toml', 'setup.py', 'package.json', 'Cargo.toml', 'go.mod',
    'CMakeLists.txt', 'Makefile', 'meson.build', 'pom.xml', 'build.gradle',
    'build.gradle.kts', '.sln', '.code-workspace',
)
_DEFAULT_EXCLUDE_DIRS = {
    '.git', '.hg', '.svn', '.idea', '.vscode', '.venv', 'venv', 'env',
    'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
    'build', 'dist', 'out', 'target', 'coverage', '.coverage', '.next', '.cache',
    'state', 'logs', 'log', 'tmp', 'temp',
}
_DEFAULT_TEXT_EXTS = {
    '.py', '.pyi', '.md', '.txt', '.rst', '.json', '.jsonc', '.toml', '.yaml', '.yml',
    '.ini', '.cfg', '.conf', '.xml', '.html', '.htm', '.css', '.scss', '.less',
    '.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.vue', '.svelte',
    '.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.java', '.kt', '.kts',
    '.go', '.rs', '.swift', '.m', '.mm', '.cs', '.fs', '.fsx', '.vb', '.sql',
    '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd', '.cmake', '.mk',
    '.gradle', '.properties', '.proto', '.graphql', '.gql',
}
_SENSITIVE_NAMES = {
    '.env', '.env.local', '.env.production', '.env.development', 'credentials.json',
    'credential.json', 'secrets.json', 'secret.json', 'local.json', 'workbot.db',
    'id_rsa', 'id_ed25519', 'known_hosts',
}
_ARCH_NAMES = {
    'agents.md', 'readme.md', 'readme.txt', 'pyproject.toml', 'package.json', 'cargo.toml',
    'go.mod', 'cmakelists.txt', 'makefile', 'meson.build', 'pom.xml', 'build.gradle',
    'build.gradle.kts', 'dockerfile', 'docker-compose.yml', 'docker-compose.yaml',
    'requirements.txt', 'environment.yml',
}


def _resolved_roots(values: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for raw in values or []:
        try:
            p = Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
        except Exception:
            continue
        if p.is_dir():
            out.append(p)
    return out


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _authorize(path: Path, roots: list[Path]) -> Path:
    resolved = path.resolve()
    if not any(_is_under(resolved, root) for root in roots):
        raise PermissionError(f'workspace path is outside configured roots: {resolved}')
    return resolved


def _has_marker(path: Path, markers: tuple[str, ...]) -> bool:
    return any((path / marker).exists() for marker in markers)


def discover_projects(configured_roots: Iterable[str], options: dict | None = None) -> dict:
    """Discover bounded project roots on the worker.

    The worker, not Windows, owns the authorization roots.  Request options may
    only make the scan *more* bounded; they never grant access outside
    ``workspace_knowledge_roots`` from the worker config.
    """
    options = options or {}
    roots = _resolved_roots(configured_roots)
    depth_limit = max(0, min(int(options.get('project_discovery_depth', 2)), 8))
    max_projects = max(1, min(int(options.get('max_projects', 20)), 200))
    markers = tuple(str(x) for x in options.get('project_markers', _DEFAULT_MARKERS))
    excludes = {str(x).lower() for x in options.get('exclude_dirs', sorted(_DEFAULT_EXCLUDE_DIRS))}
    include_root_without_marker = bool(options.get('index_root_without_marker', False))

    found: list[Path] = []
    truncated = False
    for root in roots:
        if len(found) >= max_projects:
            truncated = True
            break
        if _has_marker(root, markers):
            found.append(root)
            continue
        root_depth = len(root.parts)
        found_under_root = False
        for cur, dirs, _files in os.walk(root):
            p = Path(cur)
            depth = len(p.parts) - root_depth
            dirs[:] = sorted([d for d in dirs if d.lower() not in excludes and not d.startswith('.')], key=str.lower)
            if depth >= depth_limit:
                dirs[:] = []
            if p != root and _has_marker(p, markers):
                found.append(p)
                found_under_root = True
                dirs[:] = []
                if len(found) >= max_projects:
                    truncated = True
                    break
        if not found_under_root and include_root_without_marker and len(found) < max_projects:
            found.append(root)

    uniq: dict[str, Path] = {}
    for p in found:
        uniq[str(p)] = p
    projects = sorted(uniq.values(), key=lambda p: str(p).lower())[:max_projects]
    return {
        'roots': [str(x) for x in roots],
        'projects': [{'root_path': str(p), 'display_name': p.name or str(p)} for p in projects],
        'truncated': truncated or len(uniq) > max_projects,
    }


def _safe_file(path: Path, text_exts: set[str], markers: tuple[str, ...]) -> bool:
    name = path.name.lower()
    if name in _SENSITIVE_NAMES or name.startswith('.env'):
        return False
    if path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx', '.crt', '.cer', '.jks', '.keystore'}:
        return False
    if any(part in name for part in ('secret', 'credential', 'password', 'passwd', 'apikey', 'api_key')):
        return False
    if name.startswith('workbot.db'):
        return False
    return path.suffix.lower() in text_exts or name in _ARCH_NAMES or name in {x.lower() for x in markers if '.' in x}


def _decode_text(raw: bytes) -> str | None:
    if b'\x00' in raw[:8192]:
        return None
    for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _chunks(text: str, chunk_chars: int, max_chunks: int) -> list[str]:
    out: list[str] = []
    pos = 0
    text = text[:chunk_chars * max_chunks]
    while pos < len(text) and len(out) < max_chunks:
        end = min(len(text), pos + chunk_chars)
        if end < len(text):
            cut = text.rfind('\n', pos, end)
            if cut > pos + chunk_chars // 2:
                end = cut + 1
        chunk = text[pos:end].strip()
        if chunk:
            out.append(chunk)
        pos = max(end, pos + 1)
    return out


def scan_project(configured_roots: Iterable[str], project_root: str, options: dict | None = None,
                 known: dict | None = None) -> dict:
    """Return a bounded project manifest plus chunks for changed files only."""
    options = options or {}
    roots = _resolved_roots(configured_roots)
    if not roots:
        raise PermissionError('remote workspace_knowledge_roots is empty')
    project = _authorize(Path(project_root), roots)
    if not project.is_dir():
        raise NotADirectoryError(str(project))

    index_depth = max(0, min(int(options.get('index_path_depth', 5)), 32))
    max_files = max(20, min(int(options.get('max_files_per_project', 1200)), 20000))
    max_file_bytes = max(4096, min(int(options.get('max_file_bytes', 131072)), 4 * 1024 * 1024))
    chunk_chars = max(1000, min(int(options.get('chunk_chars', 4000)), 20000))
    max_chunks_project = max(1, min(int(options.get('max_chunks_per_project', 160)), 4000))
    max_index_chars = max(10000, min(int(options.get('max_index_chars_per_project', 600000)), 20_000_000))
    excludes = {str(x).lower() for x in options.get('exclude_dirs', sorted(_DEFAULT_EXCLUDE_DIRS))}
    markers = tuple(str(x) for x in options.get('project_markers', _DEFAULT_MARKERS))
    text_exts = {
        (str(x).lower() if str(x).startswith('.') else '.' + str(x).lower())
        for x in options.get('text_extensions', sorted(_DEFAULT_TEXT_EXTS))
    }
    known = known if isinstance(known, dict) else {}

    files_out: list[dict] = []
    inventory: list[tuple[str, int, int]] = []
    total_chunks = 0
    total_chars = 0
    truncated = False
    root_depth = len(project.parts)

    for cur, dirs, files in os.walk(project):
        pcur = Path(cur)
        rel_dir_depth = len(pcur.parts) - root_depth
        dirs[:] = sorted([d for d in dirs if d.lower() not in excludes and not d.startswith('.')], key=str.lower)
        if rel_dir_depth >= index_depth:
            dirs[:] = []
        for name in sorted(files, key=str.lower):
            path = pcur / name
            try:
                rel_path = path.relative_to(project)
            except ValueError:
                continue
            # File itself is one level below its parent directory.
            if len(rel_path.parts) > index_depth + 1:
                continue
            if not _safe_file(path, text_exts, markers):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size > max_file_bytes:
                continue
            rel = rel_path.as_posix()
            inventory.append((rel, int(st.st_size), int(st.st_mtime_ns)))
            prior = known.get(rel) if isinstance(known.get(rel), dict) else {}
            unchanged = (
                int(prior.get('size') or -1) == int(st.st_size)
                and int(prior.get('mtime_ns') or -1) == int(st.st_mtime_ns)
            )
            item = {
                'relative_path': rel,
                'size': int(st.st_size),
                'mtime_ns': int(st.st_mtime_ns),
                'file_kind': (path.suffix.lower().lstrip('.') or path.name.lower())[:32],
                'changed': not unchanged,
            }
            if unchanged:
                item['content_hash'] = str(prior.get('content_hash') or '')
            else:
                try:
                    raw = path.read_bytes()
                except OSError:
                    continue
                text = _decode_text(raw)
                if text is None:
                    continue
                item['content_hash'] = hashlib.sha256(raw).hexdigest()
                remaining_chunks = max_chunks_project - total_chunks
                remaining_chars = max_index_chars - total_chars
                if remaining_chunks > 0 and remaining_chars > 0:
                    chunks = _chunks(text[:remaining_chars], chunk_chars, remaining_chunks)
                    char_count = sum(len(x) for x in chunks)
                    item['chunks'] = chunks
                    total_chunks += len(chunks)
                    total_chars += char_count
                else:
                    item['chunks'] = []
                    truncated = True
            files_out.append(item)
            if len(files_out) >= max_files:
                truncated = True
                break
        if len(files_out) >= max_files:
            break

    fp_raw = '\n'.join(f'{r}\t{s}\t{m}' for r, s, m in sorted(inventory))
    fingerprint = hashlib.sha256(fp_raw.encode('utf-8', errors='replace')).hexdigest()
    return {
        'project_root': str(project),
        'display_name': project.name or str(project),
        'files': files_out,
        'seen_relative_paths': [r for r, _s, _m in inventory[:max_files]],
        'fingerprint': fingerprint,
        'file_count': len(inventory),
        'returned_files': len(files_out),
        'returned_chunks': total_chunks,
        'returned_chars': total_chars,
        'truncated': truncated or len(inventory) > max_files,
    }
