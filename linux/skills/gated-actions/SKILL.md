# Gated actions on a WorkBot-managed Linux task

WorkBot-managed tasks prepend `~/.workbot/enforced-bin` to `PATH`.

The wrappers currently intercept these common policy-sensitive commands:

- `git push` / `git push --force*`
- `git merge`
- `git reset --hard`
- `git clean -f...`
- `sudo ...`

Use the command normally. The wrapper will call WorkBot, show the exact argv/cwd to the approver when approval is required, and execute that exact command only after approval.

For other executable high-risk operations use:

```bash
agentctl gated-exec \
  --action deploy.production \
  --summary "Deploy service X to production" \
  -- ./deploy.sh production
```

For a non-command API/tool action, use `agentctl request-approval` before invoking that API.

Do not bypass WorkBot wrappers with `/usr/bin/git`, `/usr/bin/sudo`, a modified `PATH`, or equivalent alternate entry points. V0.9 wrappers are a managed-task enforcement hook, not a kernel sandbox; complete enforcement also requires OS/credential isolation.
