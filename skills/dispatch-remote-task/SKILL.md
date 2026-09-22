# Dispatch remote task

Use WorkBot's TaskManager rather than an ad-hoc SSH shell for delegated Agent work.

- Select the target node from explicit user wording when present; otherwise use documented topology/default-node policy.
- Every delegated task is a generic CodeAgent task. Do not invent or depend on a closed development/test/build/etc. classification.
- Preserve the intended instruction; do not silently introduce extra requirements.
- Store the origin conversation/message so completion can route back automatically.
- Ask for confirmation through a structured PendingAction before creating a side-effecting remote task.
