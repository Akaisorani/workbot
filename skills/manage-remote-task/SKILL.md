# Manage WorkBot task/workflow lifecycle

WorkBot owns lifecycle state in SQLite. Prefer WorkBot lifecycle operations over direct SSH/tmux manipulation.

Supported user-facing patterns include:

- status: `/status`;
- cancel task/workflow: `/cancel task-...`, `/cancel wf-...`;
- retry task: `/retry task-...`;
- retry workflow step: `/retry wf-... step-...`;
- steer an active task immediately without changing task/session identity: `/steer task-... <correction>`;
- append a follow-up turn to an active task: `/add task-... <instruction>`;
- continue related work after a terminal task: `/continue task-... <instruction>`;
- explicitly resume a restart-blocked workflow: `/resume wf-...`.

A retry of a workflow step invalidates downstream dependent step outputs. A Windows step interrupted by WorkBot restart is not automatically repeated because it may have had side effects.


V1.3 distinction: `/steer` supersedes the current running turn and stops its managed process scope before resuming the same CodeAgent session. `/add` does not interrupt; it waits for the current turn and then resumes the same session. `/continue` is for terminal work and may create follow-up task lineage.
