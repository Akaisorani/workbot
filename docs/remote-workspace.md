# Remote workspace knowledge (V1.7)

WorkBot can index bounded, read-only workspace knowledge from Linux nodes through the existing persistent SSH worker channel.

## Configuration

Configure roots inside the node entry:

```json
"nodes": {
  "linux-dev": {
    "ssh_alias": "linux-dev",
    "workspace_knowledge_roots": ["/home/workbot/code", "/data/repos"],
    "workspace_knowledge": {
      "project_discovery_depth": 2,
      "index_path_depth": 5,
      "max_projects": 20,
      "max_files_per_project": 1200,
      "max_chunks_per_project": 160,
      "max_index_chars_per_project": 600000
    }
  }
}
```

Or use:

```powershell
.\scripts\configure-node-workspace.ps1 -Node linux-dev -Root /home/workbot/code,/data/repos -IndexPathDepth 5
.\scripts\install-node.ps1 -Node linux-dev
```

`install-node.ps1` copies `workspace_knowledge_roots` into the worker's own `~/.workbot/config.json`. The worker rejects workspace requests outside those roots, so editing only the Windows config is not sufficient to expand access; redeploy after changing roots.

## Bounded indexing

- `project_discovery_depth`: how far below a configured root to look for project markers.
- `index_path_depth`: how far *inside a discovered project* file entries may be indexed.
- `max_projects`: maximum projects discovered on one node.
- `max_files_per_project`: maximum safe text/code files returned for a project snapshot.
- `max_chunks_per_project`: maximum content chunks sent to Windows for a project.
- `max_index_chars_per_project`: maximum changed text characters sent in one snapshot.

Remote defaults are deliberately smaller than local Windows defaults. Sensitive files and VCS/build/cache trees are excluded before content leaves the Linux node.

## Storage and search

Remote project/file paths use virtual canonical paths such as:

```text
ssh://linux-dev/home/workbot/code/gaussdb
ssh://linux-dev/home/workbot/code/gaussdb/src/gausskernel/...
```

They share the same `workspace_projects`, `workspace_files`, `workspace_chunks`, FTS and project-memory pipeline as Windows workspaces. `/workspace` shows counts per source/node and `/workspace <query>` searches all sources together.

## Scheduling

Remote scans use the same idle/night maintenance window as local workspace knowledge and alternate with local maintenance so a large local root cannot starve remote nodes. Offline nodes are skipped without blocking local indexing.
