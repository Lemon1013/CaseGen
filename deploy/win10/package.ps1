[CmdletBinding()]
param(
    [string]$OutputPath,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))

if (-not $OutputPath) {
    $OutputPath = Join-Path $repoRoot ("CaseGen-intranet-{0}.zip" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
} elseif (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path (Get-Location) $OutputPath
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
if ([IO.Path]::GetExtension($OutputPath) -ne ".zip") {
    throw "OutputPath must end with .zip"
}

$checksumPath = "$OutputPath.sha256"
if (-not $Force -and ((Test-Path -LiteralPath $OutputPath) -or (Test-Path -LiteralPath $checksumPath))) {
    throw "Output already exists. Use -Force to overwrite it."
}

function Test-ExcludedPath {
    param([string]$Path)

    $path = $Path.Replace("\", "/")
    if ($path -match '(^|/)(\.git|data|data-backups|\.venv|venv|node_modules|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.cache|\.run|secrets)(/|$)') { return $true }
    if ($path -match '(^|/)\.env($|\.)' -and $path -notmatch '(^|/)\.env\.example$') { return $true }
    if ($path -match '(^|/)(\.npmrc|\.pypirc|\.envrc|credentials\.json|id_rsa)$') { return $true }
    if ($path -match '\.(db|sqlite|sqlite3|pem|key|p12|pfx)$') { return $true }
    return [IO.Path]::GetFileName($path) -like "CaseGen-*.zip*"
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("CaseGen-snapshot-{0}" -f [guid]::NewGuid().ToString("N"))
$packageRoot = Join-Path $tempRoot "CaseGen"
$tempZip = Join-Path $tempRoot "package.zip"

try {
    New-Item -ItemType Directory -Path $packageRoot | Out-Null
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($repoRoot)

    while ($pending.Count -gt 0) {
        $sourceDirectory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $sourceDirectory -Force) {
            $relativePath = $item.FullName.Substring($repoRoot.Length).TrimStart("\", "/")
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            if (Test-ExcludedPath $relativePath) { continue }
            if ($item.FullName -eq $OutputPath -or $item.FullName -eq $checksumPath) { continue }

            $destination = Join-Path $packageRoot $relativePath
            if ($item.PSIsContainer) {
                New-Item -ItemType Directory -Path $destination -Force | Out-Null
                $pending.Push($item.FullName)
            } else {
                $destinationDirectory = Split-Path -Parent $destination
                if (-not (Test-Path -LiteralPath $destinationDirectory)) {
                    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
                }
                Copy-Item -LiteralPath $item.FullName -Destination $destination
            }
        }
    }

    Compress-Archive -LiteralPath $packageRoot -DestinationPath $tempZip -CompressionLevel Optimal

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($tempZip)
    try {
        $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        foreach ($required in @("CaseGen/backend/app/main.py", "CaseGen/deploy/win10/package.ps1")) {
            if ($entries -notcontains $required) { throw "Package validation failed: missing $required" }
        }
        foreach ($entry in $entries) {
            if (-not $entry.StartsWith("CaseGen/")) { throw "Package validation failed: invalid entry $entry" }
            $relativeEntry = $entry.Substring("CaseGen/".Length)
            if ($relativeEntry -and (Test-ExcludedPath $relativeEntry)) {
                throw "Package validation failed: blocked entry $entry"
            }
        }
    } finally {
        $archive.Dispose()
    }

    $outputDirectory = Split-Path -Parent $OutputPath
    if (-not (Test-Path -LiteralPath $outputDirectory)) {
        New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    }
    if ($Force) {
        Remove-Item -LiteralPath $OutputPath, $checksumPath -Force -ErrorAction SilentlyContinue
    }
    Copy-Item -LiteralPath $tempZip -Destination $OutputPath
    $hash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath $checksumPath -Value "$hash  $([IO.Path]::GetFileName($OutputPath))" -Encoding ASCII

    Write-Host "Created: $OutputPath"
    Write-Host "SHA256: $hash"
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
