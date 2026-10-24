# Release discipline

A release is permitted only when all conditions hold:

1. Working tree is clean and the version in `VERSION` is updated.
2. `python scripts/check_environment.py --require-engines --json` succeeds on
   the release machine.
3. `python -m pytest -q` succeeds.
4. At least one controlled real HIBLUP regression run has exit code 0,
   convergence evidence, parseable `.vars` and `.rand`, and a reviewed report.
5. Task, input checksums, engine manifest hash, Git commit, and run ID are
   recorded in the release note.
6. A reviewer approves the statistical population and model specification.

Do not tag a release from a machine with uncommitted changes or unverified
engine binaries.
