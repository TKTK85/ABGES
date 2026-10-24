# Production Readiness Audit

## Sam-style verdict

ABGES now has a credible local control plane: one audited execution kernel, authenticated role boundaries, durable queue records, an independent worker, bounded agent context, and checksum-verified recovery checkpoints. It is fit for controlled local-team operation, not yet for untrusted internet exposure or multi-node high availability.

## Evidence

| Requirement | Implemented evidence | Acceptance condition |
|---|---|---|
| Identity and authorization | `abges_mcp/control_plane.py` | SHA-256 token registry plus viewer/planner/operator/admin capability checks |
| Async execution | `workspace/control_plane/jobs`, `scripts/run_mcp_worker.py` | MCP submits; Worker invokes Harness; job state and JSONL audit persist |
| Environment isolation | `scripts/bootstrap_abges_venv.ps1` | project-local `.venv`, locked project requirements, engine manifest check |
| Observability and truthful results | job audit, run summary credibility field | only completed runs with required evidence are decision-ready |
| Resume | `state.json`, one atomically replaced JSON data checkpoint, SHA-256, `agent_result_matrix.json`, `--resume-run` | same task and unchanged input fingerprints; incomplete/failed or pending-decision runs only; successful delivery prunes the bulky checkpoint |

## Non-negotiable remaining operations controls

- Keep the stdio server local. A remote deployment needs TLS, a real identity provider, secret manager, rate limits, durable database/queue, and a separate worker account.
- Treat run-state and checkpoint files as trusted local records; filesystem write access is production authority. Completed run metadata and audit logs are retained internally, while bulky resume snapshots are pruned after delivery.
- Back up `workspace/<task_id>/_runs` and `workspace/control_plane`; they are audit records. The configured cleanup policy may delete old same-task run directories after a successful delivery.
- Do not mark a `plan`, `partial`, or `failed` result as a breeding decision.
