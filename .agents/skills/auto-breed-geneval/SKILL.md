---
name: auto-breed-geneval
description: "Use for ABGES poultry breeding genetic evaluation: phenotype, pedigree, or genotype QC; ACL/MCL/CEN/FCR derived traits; PBLUP, GBLUP, or SSGBLUP planning and HIBLUP execution; EBV, heritability, or variance-component validation and reporting. Invoke explicitly for real analyses, input audits, dry runs, result validation, or ABGES workflow troubleshooting."
---

# Automated-Breeding-Genetic-Evaluation-Skill (ABGES)

This is the Codex discovery adapter for the platform-neutral ABGES protocol. First read `../../../AGENTS.md` and `../../../docs/CONTEXT_PROTOCOL.md`. Treat the Harness as the authoritative execution path; do not reproduce genetic-evaluation logic in prompts or ad-hoc scripts.

## Safety contract

- Read `AGENTS.md`, the selected task YAML, and the matching runner before any run.
- Preserve files under `workspace/input/raw/`; use only timestamped run directories and outputs.
- Run a preflight or dry run before a real HIBLUP/PLINK operation.
- Do not call HIBLUP or PLINK directly. Use the Harness runner and retain its logs, exit code, metadata, and result JSON.
- Report `completed`, `partial`, and `failed` exactly as emitted. A placeholder runner or a test pass is not a completed genetic evaluation.
- Require user confirmation before a long-running or data-producing real analysis when it was not explicitly requested.

## Choose the path

| Need | Model / runner |
| --- | --- |
| Phenotype + pedigree | PBLUP / `runners.pblup_stage_runner:run` |
| Phenotype + PLINK genotype | GBLUP / `runners.gblup_stage_runner:run` |
| Phenotype + pedigree + PLINK genotype | SSGBLUP / `runners.ssgblup_stage_runner:run` |

Read [stage-map.md](references/stage-map.md) for stage responsibilities, gates, task files, and output contracts.

## Workflow

1. Inspect the requested task and files. Validate required phenotype columns, IDs, traits, fixed effects, and model inputs.
2. Select the task YAML under `tasks/` and the matching runner above. For derived traits, confirm their registered rule in `configs/traits/derived-traits.yaml`.
3. Run the Harness with an explicit runner. Do not omit `--runner`; omission deliberately selects a non-analysis placeholder.
4. Review `workspace/runs/<run_id>/reports/result.json`, gate reports, HIBLUP log, and registered artifacts.
5. Call the result successful only if the engine exited 0, converged, generated required outputs, passed applicable G5 checks, and raw-input checksums remained unchanged.

## Canonical command

```powershell
python harness/entrypoint/main.py <task.yaml> --runner <module:function> --mode auto
```

Use the Harness for preflight, dry runs, jobs, and diagnostics. Set `command: plan` in the
task YAML for a dry run (no engine execution) and `command: run` for the real analysis:

```powershell
python harness/entrypoint/main.py <task.yaml> --runner <module:function> --mode auto
```

> Legacy note: the archived implementation CLI (`impl/auto-breed-geneval/index.py`) was
> removed on 2026-10-13 as project-irrelevant redundancy — it was an archived legacy
> implementation with zero references from the active Harness, and the source copy is
> retained on Desktop (`Auto-Breed-GeneEval-Skill_hiblup`). Use the Harness entrypoint
> above for all preflight, job, and diagnostic operations.

## Boundaries

- Do not alter threshold, trait, pedigree, or model rules without an explicit request and a configuration review.
- Do not treat missing tests, absent fixtures, or a stale historical job as successful validation.
- Keep runner-specific details in the referenced source and documentation; read only the relevant materials for the selected model.
