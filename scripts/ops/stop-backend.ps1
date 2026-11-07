# Stop the admin server and the B8 app API started by start-backend.ps1.
. "$PSScriptRoot\common.ps1"
foreach ($name in 'admin', 'app') {
    $id = Get-Pid $name
    if ($id) { Stop-Process -Id $id -Force; Write-Output "backend: $name stopped (pid $id)" } else { Write-Output "backend: $name not running" }
    Remove-Item -LiteralPath (Join-Path $RunDir ($name + '.pid')) -ErrorAction SilentlyContinue
}
