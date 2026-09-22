# WorkBot workflows and lifecycle

A workflow is a DAG of generic Windows/remote CodeAgent steps. WorkBot decides *where and in what dependency order* work runs; each target Agent decides *how* to execute its instruction.

## V1.1 deterministic execution scope

Before the LLM planner runs, `resolve_execution_scope()` expands explicit node scope against the Node Registry. Multi-target executable requests are routed directly to Workflow planning. The planner receives `required_targets` and `fresh_execution` as hard constraints.

Coverage is validated after parsing:

```text
required: office-pc, linux-dev, linux-server2
planned:  office-pc, linux-dev
                     ^ missing linux-server2
                     -> one automatic repair
                     -> reject if still missing
```

The scope resolver does not classify task domains. It only determines the execution target set. Explicitly named nodes remain authoritative; an offline required node is not silently replaced with another node.

One user request produces one Workflow proposal/confirmation regardless of the number of child tasks.

## Runtime

Independent ready steps run concurrently. Dependent steps receive prerequisite results. Remote child tasks use the persistent SSH/agent-worker path. The Windows CodeAgent performs final synthesis after all steps succeed.

## Milestones

Progress is intentionally sparse:

- the planning Agent may mark a meaningful step completion as a milestone;
- a remote Agent may emit `task.progress` / `task.blocked` when useful;
- routine starts and child terminal events are silent at workflow level.

## V0.4 lifecycle

Useful commands:

```text
/status
/cancel task-...
/cancel wf-...
/retry task-...
/retry wf-... step-2
/continue task-... <new instruction>
/resume wf-...
```

Natural Chinese aliases such as `取消任务 ...`、`重试 ...`、`继续工作流 ...` are also recognized.

### Restart recovery

After Windows WorkBot restarts:

- completed results are reused;
- existing remote child tasks continue to be tracked;
- pending steps continue;
- a Windows step that was running at crash is changed to `interrupted`, the workflow becomes `blocked`, and WorkBot asks for explicit `/resume wf-...` before rerunning that step.

A retry of one workflow step resets that step and all downstream dependent steps, because their previous outputs may have become stale.
## Delegated requests

An operator can explicitly take over a recent executable request from another participant using wording such as `处理上面 p300... 的任务`. WorkBot resolves the durable source message before involving orchestration, then applies deterministic scope routing using the operator's current permission. The created Task/Workflow keeps the original requester and authorization provenance separately.
