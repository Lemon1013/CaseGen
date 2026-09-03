[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$repoRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$backendDir = Join-Path $repoRoot 'backend'
$frontendDir = Join-Path $repoRoot 'frontend'
$pythonPath = Join-Path $backendDir '.venv\Scripts\python.exe'
$nodeModulesPath = Join-Path $frontendDir 'node_modules'
$runDir = Join-Path $repoRoot '.run'
$pidFile = Join-Path $runDir 'pids.json'
$backendPort = 5050
$frontendPort = 5051

function Test-ProcessRunning([int]$ProcessId) {
    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Test-RecordedProcessRunning($Record) {
    if ($null -eq $Record.pid -or $null -eq $Record.started_at_utc) {
        throw 'PID record is missing required fields.'
    }
    $process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    $recordedStart = [datetime]::Parse([string]$Record.started_at_utc).ToUniversalTime()
    $actualStart = $process.StartTime.ToUniversalTime()
    return [math]::Abs(($actualStart - $recordedStart).TotalSeconds) -le 1
}

function Test-PortInUse([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $connection = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $connection.Wait(400)) {
            return $false
        }
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Stop-RecordedTree([int]$ProcessId) {
    if (Test-ProcessRunning $ProcessId) {
        & taskkill.exe /PID $ProcessId /T /F 2>$null | Out-Null
    }
}

if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Missing backend/.venv. Install backend dependencies first.'
}
if (-not (Test-Path -LiteralPath $nodeModulesPath -PathType Container)) {
    throw 'Missing frontend/node_modules. Run npm install first.'
}

if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
    try {
        $recorded = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
        $backendRunning = Test-RecordedProcessRunning $recorded.backend
        $frontendRunning = Test-RecordedProcessRunning $recorded.frontend
    }
    catch {
        throw 'Invalid .run/pids.json. Inspect or remove it, then retry.'
    }
    if ($backendRunning -and $frontendRunning) {
        Write-Host 'CaseGen is already running.'
        exit 0
    }
    if ($backendRunning -or $frontendRunning) {
        throw 'Only part of the recorded processes are running. Run .\stop.ps1 first.'
    }
    Remove-Item -LiteralPath $pidFile -Force
}

if (Test-PortInUse $backendPort) {
    throw 'Port 5050 is already in use.'
}
if (Test-PortInUse $frontendPort) {
    throw 'Port 5051 is already in use.'
}

New-Item -ItemType Directory -Path $runDir -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backendOut = Join-Path $runDir "backend-$stamp.out.log"
$backendErr = Join-Path $runDir "backend-$stamp.err.log"
$frontendOut = Join-Path $runDir "frontend-$stamp.out.log"
$frontendErr = Join-Path $runDir "frontend-$stamp.err.log"

$oldCors = [Environment]::GetEnvironmentVariable('CASEGEN_CORS_ORIGINS', 'Process')
$oldApiTarget = [Environment]::GetEnvironmentVariable('VITE_API_TARGET', 'Process')
$backendProcess = $null
$frontendProcess = $null

try {
    [Environment]::SetEnvironmentVariable(
        'CASEGEN_CORS_ORIGINS',
        'http://localhost:5051,http://127.0.0.1:5051,http://localhost:5173,http://127.0.0.1:5173',
        'Process'
    )
    $backendProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '5050') `
        -WorkingDirectory $backendDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $backendOut `
        -RedirectStandardError $backendErr `
        -PassThru

    [Environment]::SetEnvironmentVariable('VITE_API_TARGET', 'http://127.0.0.1:5050', 'Process')
    $frontendProcess = Start-Process `
        -FilePath 'npm.cmd' `
        -ArgumentList @('run', 'dev', '--', '--host', '127.0.0.1', '--port', '5051') `
        -WorkingDirectory $frontendDir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $frontendOut `
        -RedirectStandardError $frontendErr `
        -PassThru

    Start-Sleep -Milliseconds 500
    if ($backendProcess.HasExited) {
        throw "Backend failed to start. See $backendErr"
    }
    if ($frontendProcess.HasExited) {
        throw "Frontend failed to start. See $frontendErr"
    }

    $pidData = [ordered]@{
        backend = [ordered]@{
            pid = $backendProcess.Id
            started_at_utc = $backendProcess.StartTime.ToUniversalTime().ToString('o')
        }
        frontend = [ordered]@{
            pid = $frontendProcess.Id
            started_at_utc = $frontendProcess.StartTime.ToUniversalTime().ToString('o')
        }
        logs = [ordered]@{
            backend_stdout = $backendOut
            backend_stderr = $backendErr
            frontend_stdout = $frontendOut
            frontend_stderr = $frontendErr
        }
    }
    $temporaryPidFile = Join-Path $runDir 'pids.json.tmp'
    $pidData | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryPidFile -Encoding UTF8
    Move-Item -LiteralPath $temporaryPidFile -Destination $pidFile -Force
}
catch {
    if ($null -ne $frontendProcess) {
        Stop-RecordedTree $frontendProcess.Id
    }
    if ($null -ne $backendProcess) {
        Stop-RecordedTree $backendProcess.Id
    }
    throw
}
finally {
    [Environment]::SetEnvironmentVariable('CASEGEN_CORS_ORIGINS', $oldCors, 'Process')
    [Environment]::SetEnvironmentVariable('VITE_API_TARGET', $oldApiTarget, 'Process')
}

Write-Host 'CaseGen started:'
Write-Host '  Frontend http://127.0.0.1:5051'
Write-Host '  Backend  http://127.0.0.1:5050'
Write-Host 'Logs are in .run/. Run .\stop.ps1 to stop.'
