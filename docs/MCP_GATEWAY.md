# ABGES MCP Gateway Contract

## Authority boundaries

- **Agent** decides only from bounded task and evidence packets. Logical Agents are executed as `harness_stage_runner` stages; their actual executor and isolation mode are recorded in the Harness audit stream.
- **MCP** authenticates callers, grants scoped access, issues approvals, queues jobs, and returns allowlisted, bounded artifacts. It never constructs HIBLUP or PLINK commands.
- **Harness** is the only workflow executor. It owns state, gates, checkpoints, raw-input integrity checks, audit, and tool execution.

## Worker

Run a persistent local worker separately from the stdio MCP server:

```powershell
python scripts/run_mcp_worker.py --poll-seconds 1
```

Use `--once` only for diagnostics or scheduled one-job workers. Atomic file claiming permits several workers without executing the same job twice. Each claim has a lease and heartbeat; another worker requeues an expired claim up to the configured retry limit.

## Approval protocol

1. An `approver` calls `abges_approve` with the exact task YAML.
2. The task YAML must explicitly contain the same command that will be submitted; MCP rejects command overrides. MCP returns an `approval_id` and one-time `approval_token`; only its hash is persisted.
3. An `operator` submits `run` with both values. The approval is atomically consumed and must match the normalized task SHA-256.
4. `validate` and `plan` do not need approval.

A boolean `approved=true` by itself is not authorization.

## Isolation and evidence

Jobs and runs are owned by their submitting identity. Only that identity or an `admin` can read job state, run summaries, or allowlisted artifacts. MCP never exposes raw inputs, engine logs, full task YAML, or absolute artifact paths.

Every executed MCP job carries `job_id`, caller role, task SHA-256, and approval reference into `task.gateway_context`. Harness emits `mcp_harness_bound`; the resulting run audit, metadata, and control-plane JSONL can therefore be joined by job ID and task digest.
