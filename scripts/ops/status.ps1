# Read-only status: container, production schema, backend processes, HTTP health and writers (no secret printed).
param([int]$AdminPort = 8000, [int]$AppPort = 8020)
. "$PSScriptRoot\common.ps1"
Import-ProductionEnv
Write-Output ('container: ' + (Invoke-Podman @('inspect', $Container, '--format', '{{.State.Status}} health={{.State.Health.Status}}')))
$status = Get-ProductionStatus
if ($status) { Write-Output ('production schema: version {0} ready={1} ownership={2}' -f $status.schema_version, $status.ready, $status.ownership.mode) }
else { Write-Output 'production schema: NOT REACHABLE' }
foreach ($name in 'admin', 'app') { Write-Output ("{0}: pid={1}" -f $name, (Get-Pid $name)) }
Write-Output ('admin /api/status: ' + (Test-Http "http://127.0.0.1:$AdminPort/api/status"))
Write-Output ('app /api/v1/health: ' + (Test-Http "http://127.0.0.1:$AppPort/api/v1/health"))
if ($status) {
    $writers = & $Python -B -m food_trends --backend oracle-production --writer-status 2>$null | Select-Object -Last 1 | ConvertFrom-Json
    if ($writers) { foreach ($p in $writers.sources.PSObject.Properties) { Write-Output ('writer {0}: {1}' -f $p.Name, $p.Value.writer) } }
}
