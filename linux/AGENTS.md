# Linux WorkBot node instructions

This workspace belongs to one Linux development node managed by Windows WorkBot.

## Node role

Execute generic CodeAgent tasks delegated by WorkBot or started locally by the user. Decide the concrete method (read/search/edit/build/test/debug/etc.) from the authoritative instruction, repository context, node-local docs and skills. Do not invent or rely on a closed task taxonomy.

The configured node name is in `~/.workbot/config.json`. Do not assume this node is linux-dev; WorkBot V1.0 supports multiple Linux nodes.

## Managed-task isolation

When `WORKBOT_TASK_ID` is set:

- that task ID is authoritative;
- every managed task has its own CodeAgent session UUID and task-local workspace under `~/.workbot/tasks/<task-id>`;
- do not inspect/reuse sibling task directories unless the user explicitly requests historical task data;
- a worker restart reattaches the existing tmux task and must not start a replacement Agent session;
- agent-worker automatically reports final success/failure/cancellation. Do not send a duplicate final notification.
- V1.3 tasks may contain multiple instruction turns. `WORKBOT_TASK_TURN` and `WORKBOT_INSTRUCTION_SEQ` identify the active turn. A `steer` turn supersedes the previous instruction and its managed process group has been terminated; do not continue or report progress for the superseded turn. An `append` turn is a follow-up after the prior turn completed.

Sparse intermediate updates are allowed:

```bash
agentctl notify --event task.progress --summary "<meaningful phase transition>"
agentctl notify --event task.blocked --summary "<blocker requiring user attention>"
```

## Communication

Do not SSH back to the Windows office PC for normal WorkBot communication. Use local `agent-worker` through `agentctl`; outbound events/RPC travel through the existing persistent SSH relay.

For Windows-local/company-side information use the `request-workbot` skill:

```bash
agentctl read-windows 'D:\path\file.txt'
agentctl list-windows 'D:\path'
agentctl ask-workbot --instruction 'read-only question'
```

RPC permissions are per-node. If WorkBot denies a method, do not bypass it with another transport.

## Evidence/source attribution

For analytical results, keep important conclusions traceable to the evidence actually used. Cite concrete source files/symbols, documents/sections, Wiki pages/URLs, or web pages/URLs near the supported claim. Never invent provenance or guessed line numbers. When Windows/company-side evidence is obtained through `read-windows` / `ask-workbot`, preserve the source identifiers returned by WorkBot in your result so the final user-facing synthesis can retain them.

## Policy-sensitive actions

Managed tasks prepend `~/.workbot/enforced-bin` to PATH. Common sensitive git/sudo commands are routed through `agentctl gated-exec`, binding approval to exact argv/cwd. Never bypass wrappers using absolute binaries or modified PATH.

For other side effects (deploy/release, destructive deletion, production writes, external communication), use `request-approval` / `gated-actions`. Task creation confirmation is not blanket approval.

## Manually started Agent

If `WORKBOT_TASK_ID` is absent and the user explicitly asks this local Agent to report back:

```bash
~/.workbot/bin/agentctl notify --event agent.notification --summary "<concise result>"
```
