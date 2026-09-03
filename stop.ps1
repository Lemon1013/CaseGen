[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$pidFile = Join-Path (Join-Path $repoRoot '.run') 'pids.json'

if (-not (Test-Path -LiteralPath $pidFile -PathType Leaf)) {
    Write-Host 'CaseGen has no recorded running processes.'
    exit 0
}

try {
    $recorded = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $targets = @($recorded.frontend, $recorded.backend)
    foreach ($target in $targets) {
        if ($null -eq $target.pid -or $null -eq $target.started_at_utc) {
            throw 'PID record is missing required fields.'
        }
        [void][int]$target.pid
        [void][datetime]::Parse([string]$target.started_at_utc)
    }
}
catch {
    throw 'Invalid .run/pids.json. No process was stopped.'
}

$failed = @()
foreach ($target in $targets) {
    $processId = [int]$target.pid
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        continue
    }

    $recordedStart = [datetime]::Parse([string]$target.started_at_utc).ToUniversalTime()
    $actualStart = $process.StartTime.ToUniversalTime()
    if ([math]::Abs(($actualStart - $recordedStart).TotalSeconds) -gt 1) {
        Write-Warning "PID $processId was reused by another process and was skipped."
        continue
    }

    & taskkill.exe /PID $processId /T /F 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0 -and $null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        $failed += $processId
    }
}

if ($failed.Count -gt 0) {
    throw "Failed to stop these processes; PID record was kept: $($failed -join ', ')"
}

Remove-Item -LiteralPath $pidFile -Force
Write-Host 'CaseGen stopped.'
