@echo off
if defined INVOICEDOCK_PYTHON (
  "%INVOICEDOCK_PYTHON%" "%~dp0update.py"
) else (
  call conda run --no-capture-output -n invoice-desk python "%~dp0update.py"
)
exit /b %errorlevel%
