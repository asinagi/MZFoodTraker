# Stop the Oracle container cleanly (120 s grace for a normal shutdown, so the next start needs no crash recovery).
param([switch]$Force)
. "$PSScriptRoot\common.ps1"
if (-not $Force -and ((Get-Pid 'admin') -or (Get-Pid 'app'))) { Write-Output 'oracle: backend still running; run stop-backend.ps1 first (or -Force)'; exit 2 }
Invoke-Podman @('stop', '-t', '120', $Container) | Out-Null
$state = (Invoke-Podman @('inspect', $Container, '--format', '{{.State.Status}} {{.State.ExitCode}}'))
Remove-Item -LiteralPath (Join-Path $RunDir 'oracle.pid') -ErrorAction SilentlyContinue
Write-Output ('oracle: {0}' -f $state)
