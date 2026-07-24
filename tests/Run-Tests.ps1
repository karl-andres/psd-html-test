# Run-Tests.ps1 -- run the psd_html pytest suite.
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File tests\Run-Tests.ps1
#
# Requires the package installed (editable) with the test extra:
#   python -m pip install -e ".[test]"

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "########## pytest ##########"
$out = cmd /c "python -m pytest `"$here`" -v 2>&1"
$out | ForEach-Object { Write-Host $_ }

if ($LASTEXITCODE -ne 0) {
    Write-Host "FAIL: pytest" -ForegroundColor Red
    exit 1
}

Write-Host "`nAll suites passed." -ForegroundColor Green
