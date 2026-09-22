# Linux -> Windows RPC (V0.7)

V0.7 exposes synchronous, read-only requests from a trusted Linux agent-worker to the Windows WorkBot over the already-established SSH relay. Windows does not listen on a new inbound network port.

## Flow

```text
Linux CodeAgent/script
       |
    agentctl
       |
 Unix socket
       |
 agent-worker
       |
 existing persistent SSH relay (request)
       v
 Windows WorkBot RPC manager
       |
 direct file read OR fresh read-only Windows CodeAgent
       |
 existing SSH relay (response)
       v
 agent-worker -> original local agentctl process
```

The Linux worker multiplexes local requests by message ID and routes each Windows response back to the requesting local Unix-socket client. If the SSH upstream disconnects while a request is pending, that local request fails explicitly rather than hanging forever.

## Methods

- `windows.fs.read`: bounded UTF-8 text read, constrained to `windows_read_roots`;
- `windows.fs.list`: bounded directory listing, constrained to `windows_read_roots`;
- `workbot.query`: fresh one-shot Windows CodeAgent request for read/search of local/company information. It does not reuse the IM Conversation session and is instructed to perform no side effects.

RPC is disabled by default in the example configuration. Enable only trusted nodes/methods with `scripts/configure-rpc.ps1`.

## Concurrency

An RPC request is handled in a background task on Windows so a slow Windows CodeAgent lookup never blocks the SSH reader. Normal remote task events/responses keep flowing over the same SSH process while RPC is in progress.
