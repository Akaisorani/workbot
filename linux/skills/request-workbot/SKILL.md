# Request Windows WorkBot (V0.7)

Use this skill when the current Linux task needs information that belongs on the Windows office PC or in Windows-side company tools. Do **not** SSH back to Windows.

V0.7 requests are synchronous and read-only. They travel:

`Linux CodeAgent -> agentctl -> local agent-worker -> existing SSH relay -> Windows WorkBot -> response -> same Linux Agent`

## Prefer narrow direct RPC when possible

Read an allow-listed Windows text file:

```bash
agentctl read-windows 'D:\code\workbot\state\workflow-input.txt'
```

List an allow-listed Windows directory:

```bash
agentctl list-windows 'D:\code\workbot\state'
```

Generic method form:

```bash
agentctl request --method windows.fs.read \
  --data-json '{"path":"D:\\code\\workbot\\state\\workflow-input.txt"}' --text
```

## Ask the Windows Agent for read-only office-side information

Use `workbot.query` when the request needs Windows-local search or configured company information skills such as Wiki/mail/search rather than one known file path:

```bash
agentctl ask-workbot --instruction '查找Windows侧项目说明中关于Feature X的约定，并返回相关结论和来源位置'
```

For a WorkBot-managed task, `WORKBOT_TASK_ID` and `WORKBOT_CONVERSATION_ID` are attached automatically, so the Windows side can use the correct task/conversation context without the Linux Agent copying IDs manually.

## Safety / semantics

- RPC is read-only in V0.7. Do not ask it to send IM/mail, write/delete files, create/cancel tasks, deploy, push, or perform another side effect.
- Direct filesystem RPC is restricted by Windows `rpc.windows_read_roots`.
- `workbot.query` is policy-restricted by the Windows WorkBot prompt and node/method allowlist; it is intended for information retrieval, not a generic Windows shell.
- If Windows WorkBot is offline, `agentctl` returns an explicit unavailable/disconnected error. Do not loop indefinitely; report a blocker when the missing information is necessary.
