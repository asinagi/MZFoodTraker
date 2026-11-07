# Shared settings of the production operations scripts (ORACLE-PRODUCTION-CUTOVER-1). Dot-source it; it prints no secret.
# Native commands report through exit codes (checked explicitly); PowerShell 5.1 would turn their stderr into errors under Stop.
$ErrorActionPreference = 'Continue'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$EnvFile = Join-Path $Root 'data\oracle-prod\oracle-prod.env'
$OpsDir = Join-Path $Root 'data\ops-prod'
$RunDir = Join-Path $OpsDir 'run'
$LogDir = Join-Path $OpsDir 'logs'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Distro = 'Ubuntu-24.04'
$Container = 'mz-oracle-free'
New-Item -ItemType Directory -Force $RunDir, $LogDir | Out-Null

function Invoke-Podman([string[]]$Arguments) {
    & wsl.exe -d $Distro -u root -- podman @Arguments
}

function Import-ProductionEnv {
    # The production schema's settings (kept out of Git); the test schema's variables are removed so they cannot mix.
    if (-not (Test-Path -LiteralPath $EnvFile)) { throw 'production env file missing: data\oracle-prod\oracle-prod.env' }
    foreach ($line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        $text = $line.Trim()
        if (-not $text -or $text.StartsWith('#')) { continue }
        $pair = $text.Split('=', 2)
        $value = $pair[1].Trim()
        if ($value.Length -ge 2 -and $value[0] -eq $value[$value.Length - 1] -and ($value[0] -eq "'" -or $value[0] -eq '"')) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        Set-Item -Path ('Env:' + $pair[0].Trim()) -Value $value
    }
    foreach ($name in 'ORACLE_TEST_DSN', 'ORACLE_TEST_USER', 'ORACLE_TEST_PASSWORD', 'ORACLE_TEST_ENVIRONMENT', 'ORACLE_TEST_ALLOW_RESET', 'DATABASE_PATH') {
        Remove-Item -Path ('Env:' + $name) -ErrorAction SilentlyContinue
    }
    # Legacy SQLite collectors and automatic runs stay off for every process started from here.
    $env:INSTAGRAM_COLLECTION_ENABLED = 'false'; $env:YOUTUBE_COLLECTION_ENABLED = 'false'; $env:NAVER_DATALAB_ENABLED = 'false'
    $env:B3_AUTOMATIC_ENABLED = 'false'
}

function Get-ProductionStatus {
    # Read-only: the production schema's version and ownership mode (null when Oracle does not answer).
    $output = & $Python -B -m food_trends --runtime-status oracle-production 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
    return ($output | Select-Object -Last 1 | ConvertFrom-Json)
}

function Get-Pid([string]$Name) {
    $file = Join-Path $RunDir ($Name + '.pid')
    if (-not (Test-Path -LiteralPath $file)) { return $null }
    $id = [int](Get-Content -LiteralPath $file)
    if (Get-Process -Id $id -ErrorAction SilentlyContinue) { return $id }
    Remove-Item -LiteralPath $file
    return $null
}

function Test-Http([string]$Url) {
    try { return (Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri $Url).StatusCode } catch {
        if ($_.Exception.Response) { return [int]$_.Exception.Response.StatusCode } return 0
    }
}
