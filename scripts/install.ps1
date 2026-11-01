$ErrorActionPreference = "Stop"

python -m pip install --user --upgrade .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python -P -m harness setup-machine
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
