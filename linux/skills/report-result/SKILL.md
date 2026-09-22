# Report result to WorkBot

Use `agentctl` for node-local communication with `agent-worker`.

## WorkBot-managed task

If `WORKBOT_TASK_ID` is set, the current CodeAgent was launched by `agent-worker`. The worker automatically sends the authoritative `task.completed` or `task.failed` event after the process exits.

Do not send a second final-completion notification, even when the user's wording says “完成后通知我”.

You decide whether intermediate notification is useful. Send one only at a meaningful phase boundary or when user intervention is needed; avoid routine/noisy updates and normally keep milestone updates to a small number (roughly <=3 for an ordinary task).

```bash
agentctl notify --event task.progress --summary "完成关键阶段A，开始阶段B"
```

or:

```bash
agentctl notify --event task.blocked --summary "缺少测试数据路径，需要用户提供"
```

`agentctl` automatically attaches `WORKBOT_TASK_ID`.

## Manually started local agent

If `WORKBOT_TASK_ID` is not set and the user explicitly asks the local agent to report back to WorkBot:

```bash
~/.workbot/bin/agentctl notify --event agent.notification --summary "<result>"
```

The worker persists the event before acknowledging `agentctl`, so it survives a temporary Windows/SSH disconnect.
