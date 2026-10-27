# Prefer a complete Python installation over bundled application interpreters.
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$condaPython = Join-Path $env:USERPROFILE 'anaconda3/python.exe'
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -m food_trends @args
} elseif (Test-Path -LiteralPath $condaPython) {
    & $condaPython -m food_trends @args
} else {
    & python -m food_trends @args
}
exit $LASTEXITCODE
