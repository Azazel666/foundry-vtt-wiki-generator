<#
.SYNOPSIS
    Builds the Foundry VTT wiki generator Docker image, tags it, and optionally pushes it.

.DESCRIPTION
    Builds the image once and applies every requested tag, prefixed with the registry.
    The registry comes from -Registry, else from the DOCKER_REGISTRY environment variable.
    A leading http:// or https:// is dropped, because image names can't contain one.
    Without -Tag the image is tagged with the version in pyproject.toml and latest.
    Use -Push to push all tags after a successful build.

.EXAMPLE
    .\build.ps1
    Builds <registry>/foundry-vtt-wiki-generator:<version> and :latest.

.EXAMPLE
    .\build.ps1 -Push
    The same, then pushes both tags.

.EXAMPLE
    .\build.ps1 -Tag 0.2.0-rc1 -Registry registry.example.com:5000 -Push
    Builds and pushes one specific tag to another registry.
#>
[CmdletBinding()]
param(
    # One or more image tags. Default: the version in pyproject.toml, and latest.
    [string[]]$Tag = @(),

    # Registry host (and optional namespace). Default: DOCKER_REGISTRY.
    [string]$Registry = '',

    # Image repository name.
    [string]$ImageName = 'foundry-vtt-wiki-generator',

    # Build without the Docker layer cache.
    [switch]$NoCache,

    # Push all tags after building (calls push.ps1).
    [switch]$Push
)

$ErrorActionPreference = 'Stop'

function Get-Registry([string]$Value) {
    if (-not $Value) { $Value = $env:DOCKER_REGISTRY }
    if (-not $Value) { $Value = [Environment]::GetEnvironmentVariable('DOCKER_REGISTRY', 'User') }
    return ($Value -replace '^https?://', '').TrimEnd('/')
}

function Get-ProjectVersion {
    $line = Select-String -Path (Join-Path $PSScriptRoot 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"' |
        Select-Object -First 1
    if (-not $line) { throw 'No version found in pyproject.toml' }
    return $line.Matches[0].Groups[1].Value
}

function Get-ImageRef([string]$Registry, [string]$ImageName, [string]$Tag) {
    $repo = if ($Registry) { "$Registry/$ImageName" } else { $ImageName }
    return "${repo}:$Tag"
}

$Registry = Get-Registry $Registry
if ($Push -and -not $Registry) { throw 'Nothing to push to: set DOCKER_REGISTRY or pass -Registry.' }

# Accept "1.0.0,latest" as one string too (e.g. when invoked via powershell -File).
$Tag = @($Tag | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $Tag) { $Tag = @((Get-ProjectVersion), 'latest') }
$refs = @($Tag | ForEach-Object { Get-ImageRef $Registry $ImageName $_ })

$buildArgs = @('build', '--target', 'runtime')
if ($NoCache) { $buildArgs += '--no-cache' }
foreach ($ref in $refs) { $buildArgs += @('-t', $ref) }
$buildArgs += $PSScriptRoot

Write-Host "Building $($refs -join ', ')" -ForegroundColor Cyan
# Docker reports progress on stderr, which Windows PowerShell can turn into errors. The exit code is the real result.
$ErrorActionPreference = 'Continue'
& docker @buildArgs
$exitCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($exitCode -ne 0) { throw "docker build failed with exit code $exitCode" }

Write-Host 'Built:' -ForegroundColor Green
$refs | ForEach-Object { Write-Host "  $_" }

if ($Push) {
    & (Join-Path $PSScriptRoot 'push.ps1') -Tag $Tag -Registry $Registry -ImageName $ImageName
}
