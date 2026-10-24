# Team environment and engine verification

Use Python 3.10 or later on Windows. Create an isolated environment, install
`requirements-dev.txt`, then run:

```powershell
python scripts/check_environment.py --json
python scripts/check_environment.py --require-engines --json
python -m pytest -q
```

The first command verifies Python dependencies. The second additionally
requires local HIBLUP and PLINK binaries. Binaries are intentionally excluded
from Git; obtain them through the approved team distribution channel and do not
trust a binary until its SHA-256 matches `tools/ENGINE_MANIFEST.json`.

A completed evaluation also requires the engine exit code, convergence evidence,
parseable variance/EBV output, and consistent run metadata. Passing tests or a
preflight alone is not an evaluation result.
