# ABGES stage map

Read this reference after selecting an ABGES workflow.

| Stage | Responsibility | Primary implementation |
| --- | --- | --- |
| 00 | Run supervision and context | `runners/*_stage_runner.py` |
| 01 | Phenotype QC and normalization | `tools/Python/abges_phenotype.py` |
| 02 | Pedigree QC | `tools/Python/abges_pedigree.py` |
| 03 | Genotype QC | `tools/PLINK/plink_tool.py` |
| 04 | Derived traits | `tools/Python/abges_traits.py` |
| 05 | Data integration | `tools/Python/abges_integration.py` |
| 06 | Model planning | matching runner + `configs/models/` |
| 07 | Engine execution | `harness/tool-executor/`, `tools/HIBLUP/hiblup_tool.py` |
| 08 | Result validation | `tools/Python/abges_validate.py` |
| 09 | Report and metadata | matching runner + `tools/Python/abges_core.py` |

## Gate interpretation

- G1-G3 validate data admissibility, QC, and integration.
- G4 validates model plan and executable inputs.
- G5 validates engine exit, convergence, variance components, EBV, and required outputs.
- G6 validates delivery metadata, statistics, warning coverage, and raw-input checksum integrity.

## Task and runner selection

- `tasks/SAMPLE_POPULATION_ACL_pblup.yaml` -> `runners.pblup_stage_runner:run`
- `tasks/hqlhb_ail_BW8_gblup.yaml` -> `runners.gblup_stage_runner:run`
- `tasks/Virginia_BW_ssgblup.yaml` -> `runners.ssgblup_stage_runner:run`

Use `docs/AGENTS.md`, `docs/HARNESS.md`, `docs/QC_RULES.md`, and `AGENTS.md` for detailed contracts. Do not load all of them unless the task requires their domain.