# Context protocol

Use this protocol with any intelligent agent. It is independent of Codex, Claude, Cursor, Gemini, IDE extensions, and orchestration frameworks.

## Objective

Keep only decision-relevant, verifiable information in the active context. Externalize large or sensitive artifacts and retain stable paths, checksums, and summaries instead.

## Context lifecycle

1. **monitor** — measure active context against the allowed fields below.
2. **prune** — remove superseded plans, duplicate facts, completed-stage narration, and unrelated conversation.
3. **externalize** — keep raw tables, full logs, reports, images, and source files on disk; retain only path, checksum, row count, and conclusion.
4. **compact** — replace prior-stage detail with a short immutable checkpoint summary.
5. **checkpoint** — write `current_state.json`, `context_summary.md`, and append one event to `decision_log.jsonl`.
6. **restore** — load only the latest valid checkpoint and the specific referenced artifact required for the next stage.
7. **validate** — reject a context that has raw data, full logs, secrets, undocumented decisions, conflicting status, or an unbounded artifact payload.

## Allowed active context

- objective and task id;
- current stage, model, command phase, and run id;
- immutable constraints and approved decisions;
- gate verdict summaries, warnings, errors, and next action;
- artifact names, paths, checksums, counts, and concise result metrics.

## Prohibited active context

- complete phenotype, pedigree, genotype, or EBV tables;
- full HIBLUP/PLINK logs or stack traces;
- chain-of-thought, credentials, tokens, personal data not required by the next stage;
- copied documents that are available by path;
- stale run results, unverified claims, or duplicate summaries.

## Persistence layout

```text
workspace/<task_id>/_runs/<run_id>/context/
  current_state.json     # latest minimal machine-readable state
  context_summary.md     # concise human/agent handoff
  decision_log.jsonl     # append-only decisions and checkpoints
```

A checkpoint is valid only when its `run_id`, stage, status, and referenced artifact paths match the run state. The schema is `schemas/context-state-schema.yaml`.

The data blackboard checkpoint is maintained as one atomically replaced JSON snapshot (`checkpoint_blackboard.json`) with a SHA-256 recorded in `state.json`; it is not appended once per stage. Successful delivery prunes this bulky snapshot after clearing the resume pointer. Failed, interrupted, and pending-decision runs retain it.
