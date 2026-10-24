# ABGES universal agent protocol

This file is the platform-neutral operating contract for ABGES. Read it before changing code, preparing data, running an evaluation, or handing work to another agent. It applies to Codex, Claude, Cursor, Gemini, IDE agents, and human operators.

## Source of truth

1. This file defines cross-agent behavior.
2. `configs/`, `schemas/`, and task YAML files define executable rules and contracts.
3. `docs/AGENTS.md` defines the ten-stage topology.
4. Platform adapters may add invocation instructions only; they must not replace or contradict this protocol.

## Execution boundaries

- Do not edit `workspace/input/raw/`.
- Use the Harness and an explicit runner for real evaluation; do not call HIBLUP or PLINK directly.
- Run preflight validation before data-producing or long-running operations.
- Preserve timestamped outputs and report the emitted `completed`, `partial`, or `failed` status without reinterpretation.
- Do not call a test, dry run, placeholder runner, report file, or stale historical output a successful genetic evaluation.

## Context protocol

At task start, read `docs/CONTEXT_PROTOCOL.md`. For every run, create or restore `workspace/<task_id>/_runs/<run_id>/context/current_state.json`; after each stage, checkpoint a minimal summary.

Only inject into an agent context: task objective, current stage, selected model, active constraints, approved decisions, gate verdicts, artifact paths/checksums, warnings/errors, and the next action. Keep raw data, whole DataFrames, long logs, duplicate documents, secrets, and unrelated conversation outside the context window.

Use `monitor`, `prune`, `externalize`, `compact`, `checkpoint`, `restore`, and `validate` as defined in the context protocol. A handoff must cite the latest context checkpoint and decision log.

## Handoff contract

A handoff contains: objective, current stage/status, confirmed decisions, unresolved risks, artifact paths, latest checkpoint path, and exact next command or question. Do not hand off private reasoning or raw data unless the next stage requires a specific file path.
