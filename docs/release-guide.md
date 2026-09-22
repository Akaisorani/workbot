# Release guide

This document describes the cleaned release workflow used from WorkBot 1.11.2 onward.

## Before committing

Run:

```powershell
.\scripts\check-release.ps1
```

The command validates release metadata, the example config, legacy-file cleanup, Python compilation and the complete pytest suite.

Optional local runtime checks:

```powershell
.\scripts\check-release.ps1 -CodeAgent
.\scripts\check-release.ps1 -RagRuntime
```

## Never commit runtime/private data

The following belong only to a local deployment and are ignored by Git:

```text
config/local.json
state/
logs/
linux/state/
linux/logs/
.venv/
.env*
SQLite DB/WAL/SHM files
local model/cache directories
```

Do not commit passwords, tokens, private keys, enterprise credentials or copied production databases.

## Documentation policy

- `README.md`: concise current English overview.
- `README.md`: primary Chinese usage/setup document.
- `CHANGELOG.md`: all historical version/update notes in one file.
- `docs/`: current architecture/security/operations design documents.
- Historical root-level `UPDATE_V*.md` files are no longer created.

## Test policy

The full Python test suite under `tests/` is authoritative. Do not add a new `check-vXXX.ps1` for every release. Add regression tests to `tests/` and let `scripts/check-release.ps1` run the complete suite.

Use focused, version-independent diagnostics only when they exercise a real external runtime, for example `check-codeagent.ps1` or `check-session.ps1`.

## Migration/configuration scripts

Do not add one-off `configure-vXXX.ps1` migration scripts to the release tree. Extend or add stable capability-oriented scripts such as:

- `configure-access.ps1`
- `configure-action-policy.ps1`
- `configure-node.ps1`
- `configure-node-workspace.ps1`
- `configure-rpc.ps1`
- `setup-rag.ps1`

When a config schema changes, keep runtime defaults/backward compatibility in code where practical and document the new field in the example config/current docs.

## Public vs internal publication

WorkBot integrates enterprise-specific tools and may be configured with internal source/manual/Wiki paths. Before publishing outside the intended organization, independently review whether company-specific CLI names, internal URLs, documentation paths, proprietary examples or skills are permitted to be public. A software license should also be selected explicitly by the project owner; this release cleanup does not choose one automatically.
