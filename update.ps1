$ErrorActionPreference = "Stop"
$Updater = Join-Path $PSScriptRoot "update.py"
if ($env:INVOICEDOCK_PYTHON) {
    & $env:INVOICEDOCK_PYTHON $Updater
} else {
    & conda run --no-capture-output -n invoice-desk python $Updater
}
exit $LASTEXITCODE
