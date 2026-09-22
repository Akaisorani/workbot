# V0.8 Action Policy and Approval

WorkBot V0.8 separates **who may talk to the bot** (`access_control`) from **what an Agent may do without additional approval** (`action_policy`).

## Default decisions

The default policy is intentionally permissive for ordinary engineering work and conservative for external/destructive side effects.

- **allow**: read/search/status, Windows read-only RPC, workspace code edits, build/test, `git status`, `git diff`, local commit.
- **approve**: push/merge/force-push, deploy/release, sending IM/email, calendar/cloud writes, destructive/bulk deletion, writes outside the authorized workspace, production database writes, `sudo`.
- **deny**: credential export/exfiltration, disabling security controls, clearly destructive system actions.
- Unknown action names default to **approve**, not allow.

Rules are first-match glob patterns in `action_policy.rules`.

## Remote Linux flow

A Linux CodeAgent must call the local approval client before the sensitive action:

```bash
agentctl request-approval \
  --action git.push \
  --summary "Push the validated parser fix to origin/main" \
  --details-json '{"remote":"origin","branch":"main"}'
```

For an approval-required action the call blocks while WorkBot sends the originating WeLink conversation:

```text
[approval-xxxxxxxxxxxx] linux-dev 请求批准操作
动作：git.push
任务：task-...
说明：Push the validated parser fix to origin/main
批准：/approve approval-xxxxxxxxxxxx
拒绝：/deny approval-xxxxxxxxxxxx [原因]
```

Approval returns exit code 0. Denial/expiry/interruption returns exit code 3. The Agent must not execute the action unless the response contains `"approved": true`.

## Windows CodeAgent flow

Windows CodeAgent prompts instruct the Agent to stop before a policy-sensitive action and emit:

```text
<WORKBOT_APPROVAL>{"action":"git.push","summary":"Push validated change","details":{}}</WORKBOT_APPROVAL>
```

For ordinary Conversation work, WorkBot ends the current turn, requests approval, then resumes the same CodeAgent session after approval. For a Windows Workflow step, the background step waits for the approval decision and resumes after approval.

## Operator commands

```text
/approvals
/approve approval-xxxxxxxxxxxx
/deny approval-xxxxxxxxxxxx not ready for production
```

Chinese aliases are also accepted:

```text
批准 approval-xxxxxxxxxxxx
拒绝 approval-xxxxxxxxxxxx 原因
```

`action_policy.approver_senders` can restrict approval decisions to specific WeLink account IDs. If empty, any sender already authorized by inbound `access_control` can decide an approval in a conversation they can access.

## Important enforcement boundary

V0.8 is a **WorkBot control-plane policy**, not a kernel/OS sandbox. The company CodeAgent is currently invoked with `--skip-safe-check` and Linux Agents have shell access. WorkBot can reliably enforce actions that go through WorkBot APIs/`agentctl`, and prompts/skills require Agents to route high-risk shell side effects through the approval protocol, but the operating system does not technically prevent a misbehaving Agent from bypassing the helper and running a command directly.

For hard enforcement, use defense in depth: least-privilege service accounts, protected branches, deployment credentials separated from development credentials, production ACLs, container/sandbox boundaries, and command/service wrappers that require WorkBot-issued approval tokens. A later version can add those stronger enforcement layers.

## Restart semantics

Approval records are durable for audit. An approval that was still pending when WorkBot restarts is marked `interrupted`: the original synchronous Agent/RPC caller no longer exists, so WorkBot never silently replays or auto-approves it. The Agent should retry the approval request if the operation is still required.


## V0.9 exact-command gate

For executable high-risk actions, prefer `agentctl gated-exec` over a separate `request-approval` followed by an unrelated shell command. `gated-exec` automatically includes the exact argv/cwd in the approval details and only launches that argv after approval. Managed-task PATH wrappers use it for common git/sudo actions. Execution produces a durable `policy.action.executed` audit event.

The wrappers remain bypassable by a same-user process using absolute binaries or credentials directly; mandatory enforcement needs a restricted Agent identity/container and separated credentials.
