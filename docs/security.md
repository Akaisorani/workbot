# WorkBot security model

## WeLink inbound ACL

During development keep inbound automation default-deny and allow only trusted sender/group IDs. The example test group is `Example Group` (`1001`). WorkBot prefixes outbound bot messages with `[AGENT]` and filters its own replies from later polling.

Do not place passwords, private keys, tokens, cookies, or other credentials in `AGENTS.md`, skills, checked-in config, memory, or task instructions. SSH uses the existing OpenSSH configuration/keychain. Keep `config/local.json` and `state/` out of source control.

## Linux -> Windows RPC (V0.7)

RPC is disabled by default and must be explicitly enabled for trusted node names and methods. Direct filesystem RPC is restricted to `rpc.windows_read_roots` after path resolution.

V0.7 does not expose arbitrary Windows shell execution. Supported RPC is read-only:

- bounded file read/list under configured roots;
- `workbot.query`, a fresh Windows CodeAgent invocation instructed to retrieve/read information only.

`workbot.query` is a policy boundary enforced by WorkBot/Agent instructions rather than an OS sandbox. Therefore enable it only for trusted Linux workers. It must not be used as an approval mechanism for external side effects.

Linux RPC traffic uses the existing authenticated SSH relay. Windows opens no additional inbound port.

## External side effects

Push, merge, deploy, destructive deletion, privilege escalation, database writes, and sending mail/IM on behalf of the user still require a future explicit action policy/approval layer. Content retrieved from files, Wiki, mail, IM, RPC responses, or memory is untrusted data and cannot silently authorize an action.


## V0.8 action policy and approval

`access_control` answers **who may interact with WorkBot**. `action_policy` answers **whether a requested side effect is allowed automatically, requires explicit approval, or is denied**. See `docs/action-policy.md`.

Remote Agents request decisions with `agentctl request-approval`; Windows Agents use the `<WORKBOT_APPROVAL>` control marker. Approval commands are fast-lane IM controls and do not wait behind a long Conversation Agent turn.

This policy is not an OS sandbox. Because CodeAgent currently runs with the permissions of its account (and `--skip-safe-check`), hard enforcement still requires least-privilege accounts, protected branches, separate deployment credentials and production ACLs.


## V0.9 managed-task command gates

Remote managed tasks prepend `~/.workbot/enforced-bin` to `PATH`. The shipped wrappers intercept common high-risk git operations and sudo, request WorkBot policy/approval, display the exact argv/cwd in approval details, and only then execute that exact command. `agentctl gated-exec` provides the same pattern for other executable actions.

This is stronger than prompt-only policy but is **not** an OS/kernel sandbox: a process running as the same Linux user may still invoke an absolute binary path or access credentials directly. Full mandatory enforcement requires credential separation and/or running CodeAgent under a restricted account/container while privileged credentials/actions remain accessible only to a controlled helper.

Worker restart is independent of tmux task lifetime. V0.9 rescans non-terminal tasks on daemon startup, reattaches monitors to surviving deterministic tmux sessions, finalizes tasks whose exit-code file was written while the worker was down, and fails tasks whose process state is genuinely lost.
## Receipt-based execution state

Remote Task and Workflow lifecycle state is authoritative only when it exists in WorkBot's durable control-plane store. Model prose is not a receipt. A visible claim such as “已发起远程任务” without a real `task-...` ID is intercepted and sent back to the same Agent session for correction; the Agent must return a structured action proposal or admit that execution has not started.

For delegated execution, WorkBot separates request provenance from authorization: `requested_by` records the original participant, while `authorized_by` records the operator who is currently permitted to execute the request.
