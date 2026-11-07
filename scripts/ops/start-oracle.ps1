# Start the Oracle container (data on the persistent volume mz-oracle-data) and wait until the production schema answers.
. "$PSScriptRoot\common.ps1"
Import-ProductionEnv
$state = (Invoke-Podman @('inspect', $Container, '--format', '{{.State.Status}}'))
if ($state -ne 'running') {
    # An attached podman keeps the WSL session (and so the container) alive; stop-oracle.ps1 ends both.
    $process = Start-Process -FilePath 'wsl.exe' -ArgumentList '-d', $Distro, '-u', 'root', '--', 'podman', 'start', '--attach', $Container `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir 'oracle.log') -RedirectStandardError (Join-Path $LogDir 'oracle.err.log')
    Set-Content -LiteralPath (Join-Path $RunDir 'oracle.pid') -Value $process.Id
}
$deadline = (Get-Date).AddMinutes(5)
do {
    Start-Sleep -Seconds 5
    $status = Get-ProductionStatus
} until (($status -and $status.ready) -or (Get-Date) -gt $deadline)
if (-not ($status -and $status.ready)) { Write-Output 'oracle: NOT READY (production schema did not answer within 5 minutes)'; exit 2 }
Write-Output ('oracle: READY  schema version {0}, ownership {1}' -f $status.schema_version, $status.ownership.mode)
