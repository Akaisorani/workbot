# example-linux

- SSH alias: `linux-dev`
- Example Linux development node
- Worker workspace: `~/.workbot`
- WorkBot-managed tasks use a dedicated CodeAgent session UUID and task-local cwd under `~/.workbot/tasks/<task-id>`.
- Example capabilities: `linux`, `codeagent`, `ssh`, `tmux`.
- Linux→Windows communication uses `agentctl` RPC; workers do not SSH back to the Windows control plane for WorkBot communication.
