# Request WorkBot approval

Use this skill before actions that have meaningful external/destructive side effects. WorkBot V0.8 uses a configurable action policy; do not assume that a user creating a task is blanket approval for later side effects.

## When to request approval

Typical approval-required actions include `git.push`, `git.merge`, `git.force_push`, `deploy.*`, `release.*`, sending IM/email, calendar/cloud writes, bulk deletion, writes outside the authorized workspace, production database writes, or `sudo`.

Read/search/status/build/test/workspace code edits and local git status/diff/commit are normally low risk under the default policy, but the Windows policy is authoritative and may be configured differently.

## How

Before executing the sensitive action, call:

```bash
agentctl request-approval \
  --action git.push \
  --summary "Push the validated parser fix to origin/main" \
  --details-json '{"remote":"origin","branch":"main"}'
```

This call blocks until WorkBot returns a decision. The user will see an `approval-...` request in the originating WeLink conversation and can use `/approve approval-...` or `/deny approval-...`.

Exit status `0` means approved; exit status `3` means denied/expired/interrupted. Do not execute the sensitive action unless the result says `"approved": true`.

## Policy boundary

This is a WorkBot control-plane guard, not a kernel sandbox. Never bypass it by directly executing an approval-required command because shell access happens to permit it. If an action is denied, stop that action and continue only with safe work or report the block.
