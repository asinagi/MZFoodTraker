# Non-destructive smoke check of the running production backend; writes a counts-only snapshot (JSON) when -Output is given.
param([int]$AdminPort = 8000, [int]$AppPort = 8020, [string]$Output = '', [switch]$StateChange)
. "$PSScriptRoot\common.ps1"
Import-ProductionEnv
$env:MZ_ADMIN_TOKEN = (Get-Content -LiteralPath (Join-Path $RunDir 'admin-token') -ErrorAction SilentlyContinue)
$arguments = @('-B', (Join-Path $PSScriptRoot 'verify_production.py'), '--admin', "http://127.0.0.1:$AdminPort", '--app', "http://127.0.0.1:$AppPort")
if ($Output) { $arguments += @('--output', $Output) }
if ($StateChange) { $arguments += '--state-change' }  # the one identifiable admin change of the restart check
& $Python @arguments
exit $LASTEXITCODE
