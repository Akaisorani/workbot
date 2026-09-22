# Orchestrate cross-node workflow

Use a workflow whenever one executable user request spans more than one target, including multiple remote nodes or Windows + remote nodes. V1.1 deterministic scope resolution is authoritative: all-node/capability-scoped requests must not be collapsed to a single remote task.

- Steps are generic CodeAgent instructions, not domain task categories.
- Use `windows` for local PC files and office-side tools.
- Use `remote(<node>)` for node-local source, build, tests, or server files.
- Express dependencies only when later work actually needs earlier results; independent steps may run concurrently.
- Preserve user intent and do not add work merely because it is conventional.
- Select at most a few meaningful milestone notifications; routine step completion should stay silent.
- WorkBot owns task creation, result transport, and final synthesis.

- One multi-node user request gets one WorkBot Workflow confirmation; do not request separate confirmation for each child task.
- If WorkBot supplies required targets, every target must be represented by executable work in the plan.
- Fresh read/query/check requests must execute now; historical summary/memory cannot substitute for current target results.
