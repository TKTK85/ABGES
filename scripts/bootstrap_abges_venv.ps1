param([string]$Python = "python")
$ErrorActionPreference = "Stop"
& $Python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw "Virtualenv creation failed" }
& .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
if ($LASTEXITCODE -ne 0) { throw "Locked dependency installation failed" }
& .\.venv\Scripts\python.exe scripts\check_environment.py --require-engines --json
if ($LASTEXITCODE -ne 0) { throw "Environment verification failed" }
