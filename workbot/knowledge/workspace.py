from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Iterable

from workbot.storage.sqlite import Store

log = logging.getLogger(__name__)


_DEFAULT_MARKERS = (
    ".git", "pyproject.toml", "setup.py",
    "package.json", "Cargo.toml", "go.mod", "CMakeLists.txt", "Makefile", "meson.build",
    "pom.xml", "build.gradle", "build.gradle.kts", ".sln", ".code-workspace",
)
_DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "build", "dist", "out", "target", "coverage", ".coverage", ".next", ".cache",
    "state", "logs", "log", "tmp", "temp",
}
_DEFAULT_TEXT_EXTS = {
    ".py", ".pyi", ".md", ".txt", ".rst", ".json", ".jsonc", ".toml", ".yaml", ".yml",
    ".ini", ".cfg", ".conf", ".xml", ".html", ".htm", ".css", ".scss", ".less",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".java", ".kt", ".kts",
    ".go", ".rs", ".swift", ".m", ".mm", ".cs", ".fs", ".fsx", ".vb",
    ".sql", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".cmake", ".mk", ".gradle", ".properties", ".proto", ".graphql", ".gql",
}
_SENSITIVE_NAMES = {
    ".env", ".env.local", ".env.production", ".env.development", "credentials.json",
    "credential.json", "secrets.json", "secret.json", "local.json", "workbot.db",
    "id_rsa", "id_ed25519", "known_hosts",
}
_SENSITIVE_RE = re.compile(r"(?:^|[._-])(secret|secrets|credential|credentials|token|tokens|password|passwd|apikey|api_key)(?:[._-]|$)", re.I)
_ARCH_NAMES = {
    "agents.md", "readme.md", "readme.txt", "pyproject.toml", "package.json", "cargo.toml", "go.mod",
    "cmakelists.txt", "makefile", "meson.build", "pom.xml", "build.gradle", "build.gradle.kts",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "requirements.txt", "environment.yml",
}


class WorkspaceKnowledgeManager:
    """Incremental, read-only index for configured Windows read roots.

    Detailed file/chunk knowledge lives in dedicated SQLite tables.  One compact
    project-level summary is mirrored into global Memory so ordinary WorkBot
    memory retrieval can recall project identity/architecture without bloating
    ``memory_items`` with one row per file.
    """

    def __init__(self, store: Store, memory, cfg: dict | None, read_roots: Iterable[str | Path],
                 node_configs: dict | None = None, node_request=None, node_online=None):
        self.store = store
        self.memory = memory
        self.cfg = cfg or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        roots = self.cfg.get("roots") or list(read_roots or [])
        self.read_roots = [self._resolve_root(x) for x in roots if str(x).strip()]
        self.read_roots = [x for x in self.read_roots if x is not None]
        self.discovery_depth = max(0, int(self.cfg.get("project_discovery_depth", 3)))
        # File indexing depth is independent from project discovery depth.  A
        # broad read root may discover a project at depth 2, while indexing only
        # the first N levels *inside* that project keeps large trees bounded.
        self.index_path_depth = max(0, int(self.cfg.get("index_path_depth", 12)))
        self.max_projects_per_root = max(1, int(self.cfg.get("max_projects_per_root", 100)))
        self.scan_interval = max(300, int(self.cfg.get("scan_interval_seconds", 21600)))
        self.night_scan_interval = max(300, int(self.cfg.get("night_scan_interval_seconds", 3600)))
        self.night_start = int(self.cfg.get("night_start_hour", 1)) % 24
        self.night_end = int(self.cfg.get("night_end_hour", 6)) % 24
        self.allow_daytime_idle = bool(self.cfg.get("allow_daytime_idle", True))
        self.discovery_interval = max(300, int(self.cfg.get("discovery_interval_seconds", 3600)))
        self.max_files = max(20, int(self.cfg.get("max_files_per_project", 4000)))
        self.max_file_bytes = max(4096, int(self.cfg.get("max_file_bytes", 262144)))
        self.chunk_chars = max(1000, int(self.cfg.get("chunk_chars", 6000)))
        self.max_chunks_per_file = max(1, int(self.cfg.get("max_chunks_per_file", 24)))
        self.analysis_max_files = max(5, int(self.cfg.get("analysis_max_files", 36)))
        self.analysis_file_chars = max(500, int(self.cfg.get("analysis_file_chars", 5000)))
        self.analysis_max_chars = max(8000, int(self.cfg.get("analysis_max_chars", 100000)))
        self.exclude_dirs = {str(x).lower() for x in self.cfg.get("exclude_dirs", sorted(_DEFAULT_EXCLUDE_DIRS))}
        self.text_exts = {str(x).lower() if str(x).startswith(".") else "." + str(x).lower() for x in self.cfg.get("text_extensions", sorted(_DEFAULT_TEXT_EXTS))}
        self.markers = tuple(str(x) for x in self.cfg.get("project_markers", _DEFAULT_MARKERS))
        self.node_configs = dict(node_configs or {})
        self.node_request = node_request
        self.node_online = node_online or (lambda _node: False)
        self.remote_defaults = dict(self.cfg.get("remote_defaults", {}) or {})
        self._last_discovery = 0.0
        self._project_cache: list[Path] = []
        self._remote_last_discovery: dict[str, float] = {}
        self._last_maintenance_source: str | None = None

    @staticmethod
    def _resolve_root(raw: str | Path) -> Path | None:
        try:
            return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()
        except Exception:
            return None

    def _safe_file(self, path: Path) -> bool:
        name = path.name.lower()
        if name in _SENSITIVE_NAMES or name.startswith(".env"):
            return False
        if path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".jks", ".keystore"}:
            return False
        if _SENSITIVE_RE.search(name):
            return False
        if name.startswith("workbot.db"):
            return False
        return path.suffix.lower() in self.text_exts or name in _ARCH_NAMES or name in {x.lower() for x in self.markers if "." in x}

    def _has_marker(self, path: Path) -> bool:
        for marker in self.markers:
            if (path / marker).exists():
                return True
        return False

    def discover_projects(self, *, force: bool = False) -> list[Path]:
        now = time.time()
        if not force and self._project_cache and now - self._last_discovery < self.discovery_interval:
            return list(self._project_cache)
        found: list[Path] = []
        for root in self.read_roots:
            if not root.exists() or not root.is_dir():
                continue
            before = len(found)
            if self._has_marker(root):
                found.append(root)
                continue
            root_depth = len(root.parts)
            for cur, dirs, _files in os.walk(root):
                if len(found) - before >= self.max_projects_per_root:
                    break
                p = Path(cur)
                depth = len(p.parts) - root_depth
                dirs[:] = [d for d in dirs if d.lower() not in self.exclude_dirs and not d.startswith(".")]
                if depth > self.discovery_depth:
                    dirs[:] = []
                    continue
                if p != root and self._has_marker(p):
                    found.append(p)
                    dirs[:] = []
            if not any(self._is_under(p, root) for p in found):
                found.append(root)
        # deterministic, de-duplicated canonical roots
        uniq: dict[str, Path] = {}
        for p in found:
            uniq[str(p).lower()] = p
        self._project_cache = sorted(uniq.values(), key=lambda p: str(p).lower())
        self._last_discovery = now
        for p in self._project_cache:
            self.store.execute(
                "INSERT INTO workspace_projects(root_path,display_name,updated_at) VALUES (?,?,unixepoch()) "
                "ON CONFLICT(root_path) DO UPDATE SET display_name=excluded.display_name",
                (str(p), p.name or str(p)),
            )
        return list(self._project_cache)

    @staticmethod
    def _is_under(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _iter_files(self, project: Path):
        count = 0
        root_depth = len(project.parts)
        for cur, dirs, files in os.walk(project):
            pcur = Path(cur)
            rel_dir_depth = len(pcur.parts) - root_depth
            dirs[:] = [d for d in dirs if d.lower() not in self.exclude_dirs and not d.startswith(".")]
            if rel_dir_depth >= self.index_path_depth:
                dirs[:] = []
            for name in sorted(files, key=str.lower):
                p = pcur / name
                try:
                    if len(p.relative_to(project).parts) > self.index_path_depth + 1:
                        continue
                except ValueError:
                    continue
                if not self._safe_file(p):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_size > self.max_file_bytes:
                    continue
                yield p, st
                count += 1
                if count >= self.max_files:
                    return

    @staticmethod
    def _decode_text(raw: bytes) -> str | None:
        if b"\x00" in raw[:8192]:
            return None
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _chunks(self, text: str) -> list[str]:
        if not text:
            return []
        chunks=[]
        pos=0
        max_chars=self.chunk_chars * self.max_chunks_per_file
        text=text[:max_chars]
        while pos < len(text) and len(chunks) < self.max_chunks_per_file:
            end=min(len(text), pos+self.chunk_chars)
            if end < len(text):
                cut=text.rfind("\n", pos, end)
                if cut > pos + self.chunk_chars//2:
                    end=cut+1
            chunk=text[pos:end].strip()
            if chunk:
                chunks.append(chunk)
            pos=max(end, pos+1)
        return chunks

    def _project_row(self, project: Path):
        return self.store.query_one("SELECT * FROM workspace_projects WHERE root_path=?", (str(project),))

    def _due_projects(self) -> list[Path]:
        projects = self.discover_projects()
        now = int(time.time())
        hour = time.localtime().tm_hour
        night = (self.night_start <= hour < self.night_end) if self.night_start <= self.night_end else (hour >= self.night_start or hour < self.night_end)
        if not night and not self.allow_daytime_idle:
            return []
        interval = self.night_scan_interval if night else self.scan_interval
        due = []
        for p in projects:
            row = self._project_row(p)
            last = int(row["last_scanned_at"] or 0) if row else 0
            fingerprint = str(row["fingerprint"] or "") if row else ""
            analyzed = str(row["analyzed_fingerprint"] or "") if row else ""
            # If the filesystem scan succeeded but project analysis failed, retry
            # the maintenance analysis promptly instead of waiting hours for the
            # next normal scan interval.
            if not last or (fingerprint and analyzed != fingerprint) or now - last >= interval:
                due.append(p)
        return due

    def _scan_project(self, project: Path) -> dict:
        existing = {str(r["path"]): r for r in self.store.query_all("SELECT path,relative_path,size,mtime_ns,content_hash FROM workspace_files WHERE project_root=?", (str(project),))}
        seen: set[str] = set()
        changed: list[str] = []
        inventory: list[tuple[str,int,int]] = []
        for path, st in self._iter_files(project):
            pstr=str(path)
            seen.add(pstr)
            # Store logical relative paths in one cross-platform form. Absolute
            # paths/project roots remain native so Windows filesystem access keeps
            # working, but indexed relative paths always use forward slashes.
            rel=path.relative_to(project).as_posix()
            inventory.append((rel, int(st.st_size), int(st.st_mtime_ns)))
            old=existing.get(pstr)
            if old and int(old["size"] or 0)==int(st.st_size) and int(old["mtime_ns"] or 0)==int(st.st_mtime_ns):
                # V1.6.1: normalize legacy Windows rows even when file bytes did
                # not change, so old indexes converge without a forced re-read.
                if str(old["relative_path"] or "") != rel:
                    self.store.execute("UPDATE workspace_files SET relative_path=? WHERE path=?", (rel, pstr))
                continue
            try:
                raw=path.read_bytes()
            except OSError:
                continue
            text=self._decode_text(raw)
            if text is None:
                continue
            digest=hashlib.sha256(raw).hexdigest()
            kind=(path.suffix.lower().lstrip(".") or path.name.lower())[:32]
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO workspace_files(path,project_root,relative_path,size,mtime_ns,content_hash,file_kind,indexed_at) "
                    "VALUES (?,?,?,?,?,?,?,unixepoch()) ON CONFLICT(path) DO UPDATE SET project_root=excluded.project_root,relative_path=excluded.relative_path,size=excluded.size,mtime_ns=excluded.mtime_ns,content_hash=excluded.content_hash,file_kind=excluded.file_kind,indexed_at=unixepoch()",
                    (pstr,str(project),rel,int(st.st_size),int(st.st_mtime_ns),digest,kind),
                )
                conn.execute("DELETE FROM workspace_chunks WHERE path=?", (pstr,))
                for i, chunk in enumerate(self._chunks(text)):
                    conn.execute("INSERT INTO workspace_chunks(path,project_root,chunk_index,content) VALUES (?,?,?,?)", (pstr,str(project),i,chunk))
            changed.append(pstr)
        stale=set(existing)-seen
        if stale:
            with self.store.transaction() as conn:
                for pstr in stale:
                    conn.execute("DELETE FROM workspace_chunks WHERE path=?", (pstr,))
                    conn.execute("DELETE FROM workspace_files WHERE path=?", (pstr,))
        fp_raw="\n".join(f"{r}\t{s}\t{m}" for r,s,m in sorted(inventory))
        fingerprint=hashlib.sha256(fp_raw.encode("utf-8", errors="replace")).hexdigest()
        old = self._project_row(project)
        prior_fp = str(old["fingerprint"] or "") if old else ""
        prior_analyzed = str(old["analyzed_fingerprint"] or "") if old else ""
        self.store.execute(
            "INSERT INTO workspace_projects(root_path,display_name,fingerprint,file_count,last_scanned_at,updated_at) VALUES (?,?,?,?,unixepoch(),unixepoch()) "
            "ON CONFLICT(root_path) DO UPDATE SET display_name=excluded.display_name,fingerprint=excluded.fingerprint,file_count=excluded.file_count,last_scanned_at=unixepoch(),updated_at=unixepoch()",
            (str(project), project.name or str(project), fingerprint, len(inventory)),
        )
        return {
            "project": project,
            "changed": changed,
            "removed": sorted(stale),
            "fingerprint": fingerprint,
            "needs_analysis": fingerprint != prior_fp or prior_analyzed != fingerprint or not (old and str(old["summary"] or "").strip()),
            "inventory": inventory,
        }

    @staticmethod
    def _remote_virtual_root(node: str, native_root: str) -> str:
        """Return a canonical transport-neutral virtual path.

        Production remote workers are Linux, but the V1.7 worker helpers are
        also exercised directly on Windows in focused tests.  A discovered
        Windows path such as ``C:\\tmp\\repo`` must therefore round-trip
        through the ``ssh://node/...`` namespace without becoming
        ``/C:\\tmp\\repo`` on the scan request.  The virtual namespace always
        uses forward slashes; Linux absolute roots keep their leading slash,
        while drive-letter paths are represented as ``/C:/...`` in the URI.
        """
        native = str(native_root or "").strip().replace("\\", "/")
        unc = native.startswith("//")
        native = re.sub(r"/+", "/", native)
        if unc:
            native = "//" + native.lstrip("/")
        if not native:
            return f"ssh://{node}/"
        if re.match(r"^[A-Za-z]:/", native):
            return f"ssh://{node}/{native}"
        if native.startswith("//"):
            return f"ssh://{node}{native}"
        return f"ssh://{node}/" + native.lstrip("/")

    @staticmethod
    def _parse_remote_root(root_path: str) -> tuple[str, str] | None:
        m = re.match(r"^ssh://([^/]+)(/.*)$", str(root_path or ""))
        if not m:
            return None
        node, native = m.group(1), m.group(2)
        # ``ssh://node/C:/...`` is the canonical virtual representation for a
        # Windows drive path.  Drop only the URI separator slash so Path() on
        # Windows receives ``C:/...``.  Real Linux roots remain ``/home/...``.
        if re.match(r"^/[A-Za-z]:/", native):
            native = native[1:]
        return node, native

    def _remote_node_options(self, node: str) -> dict:
        cfg = dict(self.remote_defaults)
        node_cfg = dict(self.node_configs.get(node, {}) or {})
        cfg.update(dict(node_cfg.get("workspace_knowledge", {}) or {}))
        # Deliberately conservative remote defaults.  Large development servers
        # should not mirror entire source trees into the Windows control plane.
        defaults = {
            "project_discovery_depth": 2,
            "index_path_depth": 5,
            "max_projects": 20,
            "max_files_per_project": 1200,
            "max_file_bytes": 131072,
            "chunk_chars": 4000,
            "max_chunks_per_project": 160,
            "max_index_chars_per_project": 600000,
            "discovery_interval_seconds": 3600,
            "scan_interval_seconds": 21600,
            "night_scan_interval_seconds": 3600,
            "index_root_without_marker": False,
        }
        for key, value in defaults.items():
            cfg.setdefault(key, value)
        return cfg

    def remote_roots(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for node, cfg in self.node_configs.items():
            if cfg.get("enabled", True) is False:
                continue
            wk_cfg = dict(cfg.get("workspace_knowledge", {}) or {})
            if wk_cfg.get("enabled", True) is False:
                continue
            roots = [str(x).strip() for x in (cfg.get("workspace_knowledge_roots") or []) if str(x).strip()]
            if roots:
                out[str(node)] = roots
        return out

    async def _discover_remote_projects(self, node: str, *, force: bool = False) -> list[str]:
        if self.node_request is None or not self.node_online(node):
            return []
        now = time.time()
        opts = self._remote_node_options(node)
        interval = max(300, int(opts.get("discovery_interval_seconds", 3600)))
        if not force and now - self._remote_last_discovery.get(node, 0.0) < interval:
            rows = self.store.query_all(
                "SELECT root_path FROM workspace_projects WHERE root_path LIKE ? ORDER BY root_path",
                (f"ssh://{node}/%",),
            )
            return [str(r["root_path"]) for r in rows]
        try:
            result = await self.node_request(node, "workspace.discover", opts, timeout=90)
        except Exception as exc:
            log.warning("remote workspace discovery failed node=%s: %s", node, exc)
            return []
        roots: list[str] = []
        for item in result.get("projects") or []:
            native = str((item or {}).get("root_path") or "").strip()
            if not native:
                continue
            virtual = self._remote_virtual_root(node, native)
            display_native = native.replace("\\", "/")
            display = str((item or {}).get("display_name") or display_native.rstrip("/").split("/")[-1] or native)
            self.store.execute(
                "INSERT INTO workspace_projects(root_path,display_name,updated_at) VALUES (?,?,unixepoch()) "
                "ON CONFLICT(root_path) DO UPDATE SET display_name=excluded.display_name,updated_at=unixepoch()",
                (virtual, f"[{node}] {display}"),
            )
            roots.append(virtual)
        self._remote_last_discovery[node] = now
        if result.get("truncated"):
            log.info("remote workspace discovery truncated node=%s projects=%s", node, len(roots))
        return roots

    async def _due_remote_projects(self) -> list[str]:
        roots_by_node = self.remote_roots()
        if not roots_by_node or self.node_request is None:
            return []
        out: list[str] = []
        now = int(time.time())
        hour = time.localtime().tm_hour
        night = (self.night_start <= hour < self.night_end) if self.night_start <= self.night_end else (hour >= self.night_start or hour < self.night_end)
        if not night and not self.allow_daytime_idle:
            return []
        for node in roots_by_node:
            if not self.node_online(node):
                continue
            await self._discover_remote_projects(node)
            opts = self._remote_node_options(node)
            interval = max(300, int(opts.get("night_scan_interval_seconds" if night else "scan_interval_seconds", 3600 if night else 21600)))
            rows = self.store.query_all(
                "SELECT root_path,last_scanned_at,fingerprint,analyzed_fingerprint FROM workspace_projects WHERE root_path LIKE ? ORDER BY COALESCE(last_scanned_at,0),root_path",
                (f"ssh://{node}/%",),
            )
            for row in rows:
                last = int(row["last_scanned_at"] or 0)
                fp = str(row["fingerprint"] or "")
                analyzed = str(row["analyzed_fingerprint"] or "")
                if not last or (fp and fp != analyzed) or now - last >= interval:
                    out.append(str(row["root_path"]))
        return out

    async def _scan_remote_project(self, virtual_root: str) -> dict:
        parsed = self._parse_remote_root(virtual_root)
        if not parsed or self.node_request is None:
            raise ValueError(f"invalid remote workspace root: {virtual_root}")
        node, native_root = parsed
        existing_rows = self.store.query_all(
            "SELECT path,relative_path,size,mtime_ns,content_hash FROM workspace_files WHERE project_root=?",
            (virtual_root,),
        )
        existing = {str(r["relative_path"]): r for r in existing_rows}
        known = {
            rel: {"size": int(r["size"] or 0), "mtime_ns": int(r["mtime_ns"] or 0), "content_hash": str(r["content_hash"] or "")}
            for rel, r in existing.items()
        }
        opts = self._remote_node_options(node)
        result = await self.node_request(
            node, "workspace.scan",
            {"project_root": native_root, "options": opts, "known": known}, timeout=180,
        )
        seen = {str(x) for x in (result.get("seen_relative_paths") or [])}
        changed: list[str] = []
        inventory: list[tuple[str, int, int]] = []
        for item in result.get("files") or []:
            rel = str((item or {}).get("relative_path") or "").replace("\\", "/").lstrip("/")
            if not rel:
                continue
            size = int((item or {}).get("size") or 0)
            mtime_ns = int((item or {}).get("mtime_ns") or 0)
            inventory.append((rel, size, mtime_ns))
            vpath = virtual_root.rstrip("/") + "/" + rel
            digest = str((item or {}).get("content_hash") or "") or None
            kind = str((item or {}).get("file_kind") or "text")[:32]
            is_changed = bool((item or {}).get("changed", False))
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO workspace_files(path,project_root,relative_path,size,mtime_ns,content_hash,file_kind,indexed_at) "
                    "VALUES (?,?,?,?,?,?,?,unixepoch()) ON CONFLICT(path) DO UPDATE SET project_root=excluded.project_root,relative_path=excluded.relative_path,size=excluded.size,mtime_ns=excluded.mtime_ns,content_hash=COALESCE(excluded.content_hash,workspace_files.content_hash),file_kind=excluded.file_kind,indexed_at=unixepoch()",
                    (vpath, virtual_root, rel, size, mtime_ns, digest, kind),
                )
                if is_changed:
                    conn.execute("DELETE FROM workspace_chunks WHERE path=?", (vpath,))
                    for idx, chunk in enumerate((item or {}).get("chunks") or []):
                        text = str(chunk or "").strip()
                        if text:
                            conn.execute(
                                "INSERT INTO workspace_chunks(path,project_root,chunk_index,content) VALUES (?,?,?,?)",
                                (vpath, virtual_root, idx, text),
                            )
            if is_changed:
                changed.append(vpath)

        # A truncated snapshot intentionally does not claim that unseen files
        # disappeared.  This prevents limit changes from deleting previously
        # indexed deeper files and then oscillating them back later.
        removed: list[str] = []
        if not result.get("truncated"):
            stale = set(existing) - seen
            if stale:
                with self.store.transaction() as conn:
                    for rel in stale:
                        vpath = virtual_root.rstrip("/") + "/" + rel
                        conn.execute("DELETE FROM workspace_chunks WHERE path=?", (vpath,))
                        conn.execute("DELETE FROM workspace_files WHERE path=?", (vpath,))
                        removed.append(vpath)

        old = self.store.query_one("SELECT * FROM workspace_projects WHERE root_path=?", (virtual_root,))
        prior_fp = str(old["fingerprint"] or "") if old else ""
        prior_analyzed = str(old["analyzed_fingerprint"] or "") if old else ""
        fingerprint = str(result.get("fingerprint") or "")
        display = str(result.get("display_name") or native_root.rstrip("/").split("/")[-1] or native_root)
        file_count = int(result.get("file_count") or len(result.get("files") or []))
        self.store.execute(
            "INSERT INTO workspace_projects(root_path,display_name,fingerprint,file_count,last_scanned_at,updated_at) VALUES (?,?,?,?,unixepoch(),unixepoch()) "
            "ON CONFLICT(root_path) DO UPDATE SET display_name=excluded.display_name,fingerprint=excluded.fingerprint,file_count=excluded.file_count,last_scanned_at=unixepoch(),updated_at=unixepoch()",
            (virtual_root, f"[{node}] {display}", fingerprint, file_count),
        )
        return {
            "project_root": virtual_root,
            "changed": changed,
            "removed": removed,
            "fingerprint": fingerprint,
            "needs_analysis": fingerprint != prior_fp or prior_analyzed != fingerprint or not (old and str(old["summary"] or "").strip()),
            "inventory": inventory,
            "truncated": bool(result.get("truncated")),
            "node": node,
        }

    def _analysis_material_root(self, project_root: str, scan: dict) -> str:
        rows = self.store.query_all(
            "SELECT path,relative_path,size FROM workspace_files WHERE project_root=? ORDER BY relative_path",
            (project_root,),
        )
        tree = "\n".join(f"- {r['relative_path']} ({r['size']} B)" for r in rows[:800])
        changed = set(scan.get("changed") or [])
        ranked = []
        for r in rows:
            rel = str(r["relative_path"])
            # Indexed relative paths are canonical POSIX-like strings on every
            # platform.  Split explicitly so a Windows process also understands
            # remote Linux paths.
            parts = [x.lower() for x in rel.replace("\\", "/").split("/") if x]
            name = parts[-1] if parts else rel.lower()
            score = 0
            if name in _ARCH_NAMES:
                score += 100
            if str(r["path"]) in changed:
                score += 60
            if any(x in {"src", "lib", "app", "server", "client", "scripts", "docs", "tests", "test"} for x in parts):
                score += 20
            if len(parts) <= 2:
                score += 10
            ranked.append((score, rel, str(r["path"])))
        ranked.sort(key=lambda x: (-x[0], x[1].lower()))
        excerpts = []
        used = 0
        for _score, rel, path in ranked[:self.analysis_max_files]:
            chunks = self.store.query_all(
                "SELECT content FROM workspace_chunks WHERE path=? ORDER BY chunk_index LIMIT 2", (path,)
            )
            text = "\n".join(str(x["content"]) for x in chunks)
            if not text:
                continue
            text = text[:self.analysis_file_chars]
            block = f"\n### {rel}\n{text}\n"
            if used + len(block) > self.analysis_max_chars:
                break
            excerpts.append(block)
            used += len(block)
        old = self.store.query_one("SELECT * FROM workspace_projects WHERE root_path=?", (project_root,))
        prior = str(old["summary"] or "") if old else ""
        trunc = "\nNote: filesystem snapshot was bounded/truncated by workspace limits." if scan.get("truncated") else ""
        return (
            f"Project root: {project_root}{trunc}\nPrevious project summary:\n{prior or '<none>'}\n\n"
            f"Directory/file inventory:\n{tree}\n\nRepresentative file excerpts:\n{''.join(excerpts)}"
        )

    def _analysis_material(self, project: Path, scan: dict) -> str:
        return self._analysis_material_root(str(project), scan)

    def _row_last_scanned(self, root_path: str) -> int:
        row = self.store.query_one("SELECT last_scanned_at FROM workspace_projects WHERE root_path=?", (root_path,))
        return int(row["last_scanned_at"] or 0) if row else 0

    async def maintenance_once(self, agents) -> bool:
        if not self.enabled or (not self.read_roots and not self.remote_roots()):
            return False
        local_due = self._due_projects() if self.read_roots else []
        remote_due = await self._due_remote_projects()
        if not local_due and not remote_due:
            return False

        # Alternate local/remote when both have work so dozens of local projects
        # cannot starve a server index (or vice versa).  Within each source pick
        # the least-recently scanned project.
        source = "local"
        if local_due and remote_due:
            source = "remote" if self._last_maintenance_source == "local" else "local"
        elif remote_due:
            source = "remote"
        self._last_maintenance_source = source

        if source == "remote":
            remote_due.sort(key=lambda root: (self._row_last_scanned(root), root.lower()))
            project_root = remote_due[0]
            try:
                scan = await self._scan_remote_project(project_root)
            except Exception as exc:
                log.warning("remote workspace scan failed project=%s: %s", project_root, exc)
                return True
        else:
            local_due.sort(key=lambda p: (self._row_last_scanned(str(p)), str(p).lower()))
            project = local_due[0]
            scan = await __import__('asyncio').to_thread(self._scan_project, project)
            project_root = str(project)

        if not scan["needs_analysis"]:
            log.info("workspace knowledge scan unchanged project=%s files=%s", project_root, len(scan["inventory"]))
            return True
        material = await __import__('asyncio').to_thread(self._analysis_material_root, project_root, scan)
        analysis = await agents.analyze_workspace_project(project_root, material)
        if not analysis:
            return True
        summary = str(analysis.get("summary") or "").strip()
        structure = str(analysis.get("structure") or "").strip()
        keywords = analysis.get("keywords") or []
        if isinstance(keywords, str):
            keywords = [x.strip() for x in keywords.split(",") if x.strip()]
        keyword_text = ", ".join(str(x).strip() for x in keywords[:30] if str(x).strip())
        self.store.execute(
            "UPDATE workspace_projects SET summary=?,structure=?,keywords=?,analyzed_fingerprint=fingerprint,last_analyzed_at=unixepoch(),updated_at=unixepoch() WHERE root_path=?",
            (summary, structure, keyword_text, project_root),
        )
        if summary:
            content = summary
            if structure:
                content += "\nArchitecture: " + structure
            content += f"\nRoot: {project_root}"
            if keyword_text:
                content += "\nKeywords: " + keyword_text
            display_row = self.store.query_one("SELECT display_name FROM workspace_projects WHERE root_path=?", (project_root,))
            display = str(display_row["display_name"] or project_root) if display_row else project_root
            source_kind = "remote node workspace" if project_root.startswith("ssh://") else "configured windows_read_roots"
            self.memory.upsert_source(
                "global", f"Workspace project: {display}", content,
                tags="workspace,project," + keyword_text, source=f"workspace-project:{project_root}",
                memory_kind="workspace", confidence=0.9,
                evidence=f"Read-only incremental scan of {source_kind} project {project_root}", auto_generated=True,
            )
        log.info(
            "workspace knowledge updated project=%s files=%s changed=%s%s",
            project_root, len(scan["inventory"]), len(scan["changed"]), " truncated" if scan.get("truncated") else "",
        )
        return True

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        """Extract useful project/path/symbol terms from natural-language queries.

        Workspace lookup must work for requests such as "bot 帮我找 WorkBot 里
        WeLink 的轮询代码" rather than only exact full-sentence LIKE matches.
        ASCII identifiers/path fragments are especially valuable; conservative
        CJK n-grams cover common Chinese component names without indexing every
        two-character chatter fragment.
        """
        q = " ".join(str(query or "").split())
        if not q:
            return []
        out: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            value = value.strip(" \t\r\n,，。:：;；!?！？()（）[]【】{}<>\"'")
            key = value.lower()
            if len(value) < 2 or key in seen:
                return
            seen.add(key)
            out.append(value)

        # Repo names, paths, symbols and English technical terms.
        for token in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.\\/\-:]{1,}", q):
            add(token)
            # A full Windows/path token may be too specific; expose basename-ish
            # pieces as independent retrieval hints as well.
            for part in re.split(r"[\\/:]+", token):
                if len(part) >= 2:
                    add(part)

        cjk_stop = {
            "帮我", "帮忙", "请问", "查看", "查找", "查询", "搜索", "分析", "理解",
            "一下", "相关", "本地", "文件", "目录", "项目", "代码", "里面", "中的",
            "这个", "那个", "哪里", "在哪", "怎么", "如何", "是什么",
        }
        for seq in re.findall(r"[\u4e00-\u9fff]{2,}", q):
            if seq in cjk_stop:
                continue
            if len(seq) <= 6:
                add(seq)
            else:
                # 3/4-char grams tend to capture Chinese component/module names
                # (e.g. 工作流, 调度器, 消息接收) while avoiding noisy bigrams.
                for width in (4, 3):
                    for i in range(0, len(seq) - width + 1):
                        gram = seq[i:i + width]
                        if gram not in cjk_stop:
                            add(gram)
                        if len(out) >= 12:
                            break
                    if len(out) >= 12:
                        break
            if len(out) >= 12:
                break
        return out[:12]

    @classmethod
    def _fts_query(cls, query: str) -> str:
        terms = cls._query_terms(query)
        return " OR ".join(f'"{x.replace(chr(34), "")}"' for x in terms[:12])

    @staticmethod
    def _score_hit(text: str, terms: list[str], *, path_weight: int = 1) -> int:
        low = (text or "").lower()
        score = 0
        for term in terms:
            t = term.lower()
            if not t:
                continue
            occurrences = low.count(t)
            if occurrences:
                score += (8 + min(occurrences, 4)) * path_weight
        return score

    def _root_priority_score(self, project_root: str) -> int:
        """Prefer explicit/specific roots over broad archival roots.

        Root order is operator intent.  This makes D:\\code\\workbot rank ahead
        of old extracted WorkBot releases under Downloads when both match.
        """
        project_root = str(project_root or "")
        parsed = self._parse_remote_root(project_root)
        if parsed:
            node, native = parsed
            roots = self.remote_roots().get(node, [])
            for idx, raw in enumerate(roots):
                base = str(raw).rstrip("/")
                if native == base or native.startswith(base + "/"):
                    return max(0, 70 - idx * 8)
            return 10
        try:
            p = Path(project_root).resolve()
        except Exception:
            return 0
        for idx, root in enumerate(self.read_roots):
            if self._is_under(p, root):
                # Earlier, more specific configured roots win.
                specificity = min(len(root.parts), 12)
                return max(0, 90 - idx * 10 + specificity)
        return 0

    def search(self, query: str, limit: int = 8) -> list[dict]:
        query = (query or "").strip()
        if not self.enabled or not query:
            return []
        terms = self._query_terms(query)
        if not terms:
            return []

        candidates: dict[tuple[str, str], dict] = {}

        # Project summaries/root names are cheap to search and often the best
        # answer when the user asks for a project background rather than a symbol.
        for term in terms[:8]:
            q = f"%{term}%"
            rows = self.store.query_all(
                "SELECT root_path,display_name,summary,structure,keywords FROM workspace_projects "
                "WHERE display_name LIKE ? OR root_path LIKE ? OR summary LIKE ? OR structure LIKE ? OR keywords LIKE ? "
                "ORDER BY updated_at DESC LIMIT 30",
                (q, q, q, q, q),
            )
            for r in rows:
                key = ("project", str(r["root_path"]))
                entry = candidates.setdefault(key, {
                    "type": "project", "path": str(r["root_path"]), "project_root": str(r["root_path"]),
                    "excerpt": (f"{r['summary'] or ''}\n{r['structure'] or ''}").strip()[:2000], "score": 0,
                })
                entry["score"] += (
                    self._score_hit(str(r["root_path"]), [term], path_weight=4)
                    + self._score_hit(str(r["display_name"]), [term], path_weight=5)
                    + self._score_hit(str(r["keywords"] or ""), [term], path_weight=3)
                    + self._score_hit(str(r["summary"] or "") + " " + str(r["structure"] or ""), [term])
                )

        # FTS gives good content/symbol retrieval where available.
        if self.store.workspace_fts_available:
            fts = self._fts_query(query)
            if fts:
                try:
                    rows = self.store.query_all(
                        "SELECT c.path,c.project_root,c.content,bm25(workspace_fts) AS rank "
                        "FROM workspace_fts JOIN workspace_chunks c ON c.id=workspace_fts.rowid "
                        "WHERE workspace_fts MATCH ? ORDER BY rank LIMIT ?",
                        (fts, int(max(limit * 8, 30))),
                    )
                    for r in rows:
                        key = ("file", str(r["path"]))
                        entry = candidates.setdefault(key, {
                            "type": "file", "path": str(r["path"]), "project_root": str(r["project_root"]),
                            "excerpt": str(r["content"] or "")[:1600], "score": 0,
                        })
                        entry["score"] += 40 + self._score_hit(str(r["path"]), terms, path_weight=3)
                except Exception:
                    pass

        # LIKE supplements FTS for paths and corporate SQLite builds without FTS5.
        for term in terms[:8]:
            q = f"%{term}%"
            rows = self.store.query_all(
                "SELECT f.path,f.project_root,f.relative_path,c.content FROM workspace_files f "
                "LEFT JOIN workspace_chunks c ON c.path=f.path AND c.chunk_index=0 "
                "WHERE f.path LIKE ? OR f.relative_path LIKE ? OR c.content LIKE ? "
                "ORDER BY f.indexed_at DESC LIMIT 40",
                (q, q, q),
            )
            for r in rows:
                key = ("file", str(r["path"]))
                entry = candidates.setdefault(key, {
                    "type": "file", "path": str(r["path"]), "project_root": str(r["project_root"]),
                    "excerpt": str(r["content"] or "")[:1600], "score": 0,
                })
                entry["score"] += (
                    self._score_hit(str(r["path"]), [term], path_weight=4)
                    + self._score_hit(str(r["relative_path"]), [term], path_weight=5)
                    + self._score_hit(str(r["content"] or ""), [term])
                )

        for entry in candidates.values():
            entry["score"] = int(entry.get("score", 0)) + self._root_priority_score(str(entry.get("project_root") or ""))

        ranked = sorted(
            candidates.values(),
            key=lambda h: (-int(h.get("score", 0)), 0 if h.get("type") == "project" else 1, str(h.get("path", "")).lower()),
        )
        for h in ranked:
            h.pop("score", None)
        return ranked[:limit]

    def force_rescan(self) -> None:
        self._project_cache = []
        self._last_discovery = 0.0
        self._remote_last_discovery.clear()
        self.store.execute("UPDATE workspace_projects SET last_scanned_at=NULL")

    def status(self) -> dict:
        projects = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_projects")
        files = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_files")
        chunks = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_chunks")
        pending = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_projects WHERE fingerprint IS NOT NULL AND COALESCE(analyzed_fingerprint,'')<>fingerprint")
        local_projects = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_projects WHERE root_path NOT LIKE 'ssh://%'")
        local_files = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_files WHERE project_root NOT LIKE 'ssh://%'")
        local_chunks = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_chunks WHERE project_root NOT LIKE 'ssh://%'")
        sources = {
            "local": {
                "projects": int(local_projects["n"] if local_projects else 0),
                "files": int(local_files["n"] if local_files else 0),
                "chunks": int(local_chunks["n"] if local_chunks else 0),
            }
        }
        for node in self.remote_roots():
            like = f"ssh://{node}/%"
            p = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_projects WHERE root_path LIKE ?", (like,))
            f = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_files WHERE project_root LIKE ?", (like,))
            c = self.store.query_one("SELECT COUNT(*) AS n FROM workspace_chunks WHERE project_root LIKE ?", (like,))
            sources[node] = {
                "projects": int(p["n"] if p else 0),
                "files": int(f["n"] if f else 0),
                "chunks": int(c["n"] if c else 0),
                "online": bool(self.node_online(node)),
            }
        return {
            "enabled": self.enabled,
            "roots": [str(x) for x in self.read_roots],
            "remote_roots": self.remote_roots(),
            "projects": int(projects["n"] if projects else 0),
            "files": int(files["n"] if files else 0),
            "chunks": int(chunks["n"] if chunks else 0),
            "pending_analysis": int(pending["n"] if pending else 0),
            "fts": bool(self.store.workspace_fts_available),
            "sources": sources,
        }

