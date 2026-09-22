# Node topology and routing (V1.0)

WorkBot is a Windows control plane with zero or more Linux worker nodes.

```text
WeLink
  |
  v
Windows WorkBot / office-pc
  |- Node Registry (static capabilities + runtime online/load)
  |- Conversation / Task / Workflow / Memory / Approval
  |
  +-- persistent SSH -> node A agent-worker -> tmux -> CodeAgent
  +-- persistent SSH -> node B agent-worker -> tmux -> CodeAgent
  `-- persistent SSH -> node N agent-worker -> tmux -> CodeAgent
```

A node entry can contain:

```json
{
  "ssh_alias": "linux-dev",
  "description": "ARM64 Linux development node",
  "capabilities": ["linux", "aarch64", "codeagent"],
  "labels": ["arm64", "euleros"],
  "routing_hints": ["arm", "aarch64"],
  "max_concurrent": 4,
  "priority": 100,
  "enabled": true
}
```

Capabilities are environmental facts, not task classes. Examples: `aarch64`, `cuda`, `gpu`, `docker`, `large-memory`, or a repository/tool name.

Routing rules:

1. An explicitly named configured node is authoritative.
2. Otherwise the reasoning/planning Agent may pick a node from the current catalog or request `node=auto`.
3. Auto selection only considers enabled online nodes, prefers matching capability/label/hint text, then lower load/default-node/priority.
4. If no node is online, WorkBot does not silently pretend a task was dispatched.
5. RPC permissions remain separate: adding a node does not automatically authorize that node to read Windows data.

Use `/nodes` in WeLink for current online/load/capability state.
