# WorkBot communication

Keep dialogue state, workflow state, and execution state separate.

- If an operation requires confirmation, store a structured `PendingAction`; a short later reply such as `创建`/`确认` executes that saved action rather than asking an Agent to infer the missing object again.
- A direct remote task is one generic CodeAgent instruction on one node.
- A Workflow is a DAG of Windows/remote CodeAgent steps with explicit dependencies and one final synthesis.
- Task/workflow IDs are authoritative references for status and follow-up.
- `task.progress`/`task.blocked` are optional Agent-selected intermediate events; terminal task state comes from agent-worker.

- An active Task may contain multiple instruction turns. Corrections use `steer`; post-current follow-ups use `append`. Preserve one task ID and one CodeAgent session across those turns.
