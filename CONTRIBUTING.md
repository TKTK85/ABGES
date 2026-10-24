# Contributing to ABGES

## Change discipline

- Do not modify raw input data. Use run-local derived files only.
- Keep HIBLUP and PLINK execution behind the Harness tool executor.
- Add or update a regression test for every change in a gate, statistical
  population rule, task command mode, or result parser.
- Run `python scripts/check_environment.py --json` and `python -m pytest -q`
  before committing.
- Do not commit `workspace/`, source data, generated outputs, or engine binaries.

## Review discipline

A reviewer must confirm the task's target population, trait definition, model,
fixed effects, and reporting population before `command: run` is approved.
Use `validate` for a no-engine preflight and `plan` to review the exact QC and
HIBLUP command without executing HIBLUP.

## Release discipline

Follow `docs/RELEASE.md`. Every release note must name the Git commit, engine
manifest SHA-256, test result, and at least one verified real-run evidence path.
