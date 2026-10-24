# ABGES command modes

## validate

`command: validate` is a pure preflight. It runs only task, phenotype, and
applicable pedigree checks (stages 00--02). It never invokes PLINK or HIBLUP,
and it deliberately returns `partial` because no engine result has been
validated. A preflight may write a run-local QC report and an engine-format
phenotype preview; these are not analysis outputs.

## plan

`command: plan` runs the full applicable data QC, integration, engine-availability
check and command assembly through stage 06. It may invoke PLINK QC and write
run-local planning artifacts, but it never invokes HIBLUP and therefore returns
`partial`. Use it when a reviewer needs to approve the exact model and command
before computation.

## run

`command: run` executes the complete applicable pipeline. Only a completed run
with an engine exit code of zero, convergence evidence, and parseable result
artifacts may be reported as a completed genetic evaluation.

## status

`command: status` reads recorded run state. It does not start a pipeline.
