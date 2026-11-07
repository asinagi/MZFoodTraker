# Start the admin server and the B8 app API on the production schema (local addresses only, no collector, no scheduler).
param([int]$AdminPort = 8000, [int]$AppPort = 8020)
. "$PSScriptRoot\common.ps1"
Import-ProductionEnv
foreach ($name in 'admin', 'app') { if (Get-Pid $name) { Write-Output "backend: $name already running (pid $(Get-Pid $name)); not started twice"; exit 2 } }
foreach ($port in $AdminPort, $AppPort) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { Write-Output "backend: port $port is in use"; exit 2 }
}
$status = Get-ProductionStatus
if (-not ($status -and $status.ready)) { Write-Output 'backend: production schema not ready (run start-oracle.ps1)'; exit 2 }
$tokenFile = Join-Path $RunDir 'admin-token'
if (-not (Test-Path -LiteralPath $tokenFile)) { Set-Content -LiteralPath $tokenFile -Value ([guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')) }
$env:MZ_ADMIN_TOKEN = (Get-Content -LiteralPath $tokenFile)
$env:PORT = [string]$AdminPort
$admin = Start-Process -FilePath $Python -ArgumentList '-B', '-m', 'food_trends', '--backend', 'oracle-production', '--no-schedulers' -WorkingDirectory $Root `
    -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'admin.log') -RedirectStandardError (Join-Path $LogDir 'admin.err.log')
Set-Content -LiteralPath (Join-Path $RunDir 'admin.pid') -Value $admin.Id
$app = Start-Process -FilePath $Python -ArgumentList '-B', '-m', 'food_trends.app_api', '--store', 'oracle-production', '--port', "$AppPort" `
    -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'app.log') -RedirectStandardError (Join-Path $LogDir 'app.err.log')
Set-Content -LiteralPath (Join-Path $RunDir 'app.pid') -Value $app.Id
$deadline = (Get-Date).AddSeconds(90)
do {
    Start-Sleep -Seconds 2
    $codes = @((Test-Http "http://127.0.0.1:$AdminPort/api/status"), (Test-Http "http://127.0.0.1:$AppPort/api/v1/health"))
} until (($codes[0] -eq 200 -and $codes[1] -eq 200) -or (Get-Date) -gt $deadline -or -not (Get-Pid 'admin') -or -not (Get-Pid 'app'))
if ($codes[0] -ne 200 -or $codes[1] -ne 200) { Write-Output ('backend: NOT HEALTHY admin={0} app={1} (see data\ops-prod\logs)' -f $codes[0], $codes[1]); exit 2 }
Write-Output "backend: admin http://127.0.0.1:$AdminPort  app http://127.0.0.1:$AppPort/api/v1  (production schema $($env:ORACLE_PRODUCTION_SCHEMA))"
