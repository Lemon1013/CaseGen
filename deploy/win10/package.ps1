[CmdletBinding()]
param(
    [string]$OutputPath,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
if (-not $OutputPath) {
    $OutputPath = Join-Path $repoRoot ("CaseGen-intranet-win10-py311-{0}.zip" -f (Get-Date -Format "yyyyMMdd"))
} elseif (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $repoRoot $OutputPath
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$checksumPath = "$OutputPath.sha256"

if ([IO.Path]::GetExtension($OutputPath) -ne ".zip") {
    throw "OutputPath must end in .zip"
}
if (-not $Force -and ((Test-Path -LiteralPath $OutputPath) -or (Test-Path -LiteralPath $checksumPath))) {
    throw "Output already exists. Use -Force to replace it: $OutputPath"
}

foreach ($command in "git", "npm", "python") {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $command"
    }
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("CaseGen-package-{0}" -f [guid]::NewGuid().ToString("N"))
$packageRoot = Join-Path $tempRoot "CaseGen"
$tempZip = Join-Path $tempRoot "CaseGen.zip"

function Test-ExcludedPath([string]$Path) {
    $path = $Path.Replace("\", "/")
    if ($path -match '(^|/)(\.git|data|\.run|node_modules|\.venv|__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.cache)(/|$)') { return $true }
    if ($path -match '^frontend/dist(/|$)') { return $true }
    if ($path -match '(^|/)\.env($|\.)' -and $path -notmatch '(^|/)\.env\.example$') { return $true }
    if ($path -match '(^|/)\.npmrc$|(^|/)notes/customer-export\.csv$') { return $true }
    if ($path -match '\.(db|sqlite|sqlite3|pem|key|p12|pfx)$') { return $true }
    if ($path -match '(^|/)secrets(/|$)|(^|/)(id_rsa|credentials\.json)$') { return $true }
    return $path -match '\.zip($|\.)|\.sha256$'
}

try {
    New-Item -ItemType Directory -Path $packageRoot -Force | Out-Null

    & git -C $repoRoot rev-parse --is-inside-work-tree | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Not a Git work tree: $repoRoot" }

    & python --version
    if ($LASTEXITCODE -ne 0) { throw "python is not runnable" }
    & python -m pip --version
    if ($LASTEXITCODE -ne 0) { throw "pip is not available for python" }

    Write-Host "Building frontend..."
    Push-Location (Join-Path $repoRoot "frontend")
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
    } finally {
        Pop-Location
    }

    $distRoot = Join-Path $repoRoot "frontend\dist"
    if (-not (Test-Path -LiteralPath (Join-Path $distRoot "index.html") -PathType Leaf)) {
        throw "Frontend build did not create frontend\dist\index.html"
    }

    Write-Host "Collecting work tree files..."
    $files = @(& git -C $repoRoot -c core.quotePath=false ls-files --cached)
    if ($LASTEXITCODE -ne 0) { throw "git ls-files failed" }
    foreach ($relativePath in $files) {
        if (-not $relativePath -or (Test-ExcludedPath $relativePath)) { continue }
        $source = [IO.Path]::GetFullPath((Join-Path $repoRoot $relativePath))
        if ($source -eq $OutputPath -or $source -eq $checksumPath -or -not (Test-Path -LiteralPath $source -PathType Leaf)) { continue }
        $destination = Join-Path $packageRoot $relativePath
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }

    $packageScript = Join-Path $packageRoot "deploy\win10\package.ps1"
    New-Item -ItemType Directory -Path (Split-Path -Parent $packageScript) -Force | Out-Null
    Copy-Item -LiteralPath $PSCommandPath -Destination $packageScript -Force

    $packageDist = Join-Path $packageRoot "frontend\dist"
    New-Item -ItemType Directory -Path $packageDist -Force | Out-Null
    Get-ChildItem -LiteralPath $distRoot -Force | Copy-Item -Destination $packageDist -Recurse -Force

    Write-Host "Downloading Windows x64 / CPython 3.11 wheels..."
    $requirements = Join-Path $packageRoot "deploy\win10\requirements-offline.txt"
    $wheelRoot = Join-Path $packageRoot "deploy\win10\backend_wheels"
    New-Item -ItemType Directory -Path $wheelRoot -Force | Out-Null
    & python -m pip download --requirement $requirements --dest $wheelRoot --only-binary=:all: --platform win_amd64 --python-version 311 --implementation cp --abi cp311
    if ($LASTEXITCODE -ne 0) { throw "pip download failed" }
    $wheelCount = @(Get-ChildItem -LiteralPath $wheelRoot -Filter "*.whl" -File).Count
    if ($wheelCount -eq 0) { throw "No wheels were downloaded" }

    $commit = (& git -C $repoRoot rev-parse HEAD).Trim()
    $branch = (& git -C $repoRoot branch --show-current).Trim()
    if (-not $branch) { $branch = "detached HEAD" }
    $dirty = if (& git -C $repoRoot status --porcelain) { "yes" } else { "no" }
    $buildInfo = @(
        "CaseGen Intranet Windows 10 Offline Package",
        "",
        "Source commit: $commit",
        "Source branch: $branch",
        "Dirty work tree: $dirty",
        "Build time: $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss zzz'))",
        "Target: Windows 10 x64, CPython 3.11",
        "Wheel count: $wheelCount",
        "",
        "Install Python 3.11 x64, then run deploy\win10\install.bat and deploy\win10\run.bat."
    )
    [IO.File]::WriteAllLines((Join-Path $packageRoot "BUILD_INFO.txt"), $buildInfo, [Text.UTF8Encoding]::new($false))

    Write-Host "Creating ZIP..."
    Compress-Archive -LiteralPath $packageRoot -DestinationPath $tempZip -CompressionLevel Optimal

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($tempZip)
    try {
        $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        $required = @(
            "CaseGen/backend/app/main.py",
            "CaseGen/frontend/dist/index.html",
            "CaseGen/deploy/win10/install.bat",
            "CaseGen/deploy/win10/run.bat",
            "CaseGen/deploy/win10/requirements-offline.txt"
        )
        $missing = @($required | Where-Object { $entries -notcontains $_ })
        $blocked = @($entries | Where-Object {
            $relativePath = $_ -replace '^CaseGen/', ''
            $relativePath -notmatch '^frontend/dist(/|$)' -and (Test-ExcludedPath $relativePath)
        })
        $zippedWheels = @($entries | Where-Object { $_ -like "CaseGen/deploy/win10/backend_wheels/*.whl" })
        if ($missing.Count -or $blocked.Count -or $zippedWheels.Count -ne $wheelCount) {
            throw "ZIP validation failed (missing=$($missing.Count), blocked=$($blocked.Count), wheels=$($zippedWheels.Count)/$wheelCount)"
        }
    } finally {
        $archive.Dispose()
    }

    $outputDirectory = Split-Path -Parent $OutputPath
    New-Item -ItemType Directory -Path $outputDirectory -Force | Out-Null
    [IO.File]::Copy($tempZip, $OutputPath, $true)
    $hash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText($checksumPath, "$hash  $([IO.Path]::GetFileName($OutputPath))`r`n", [Text.Encoding]::ASCII)

    Write-Host "Created: $OutputPath"
    Write-Host "SHA256: $hash"
    Write-Host "Wheels: $wheelCount"
} finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
