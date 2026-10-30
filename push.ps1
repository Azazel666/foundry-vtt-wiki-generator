<#
.SYNOPSIS
    Pushes previously built Foundry VTT wiki generator image tags to a registry.

.DESCRIPTION
    Pushes <Registry>/<ImageName>:<Tag> for every tag. The images must already exist
    locally (run build.ps1 with the same -Tag/-Registry first). Log in beforehand with
    `docker login <registry>` if the registry requires authentication.
    The registry comes from -Registry, else from the DOCKER_REGISTRY environment variable.
    Without -Tag the version in pyproject.toml and latest are pushed.
    Version tags are immutable: a tag already in the registry is refused, except latest,
    unless -Force is given.

.EXAMPLE
    .\push.ps1

.EXAMPLE
    .\push.ps1 -Tag 1.0.0, latest -Registry registry.example.com:5000
#>
[CmdletBinding()]
param(
    # One or more image tags to push. Default: the version in pyproject.toml, and latest.
    [string[]]$Tag = @(),

    # Registry host (and optional namespace). Default: DOCKER_REGISTRY.
    [string]$Registry = '',

    # Image repository name.
    [string]$ImageName = 'foundry-vtt-wiki-generator',

    # Overwrite a version tag that is already in the registry.
    [switch]$Force
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

function Test-RemoteTag([string]$Ref) {
    # Windows PowerShell turns a native command's stderr into errors, so this can't run under 'Stop'.
    $ErrorActionPreference = 'Continue'
    & docker manifest inspect $Ref 2>&1 | Out-Null
    return $LASTEXITCODE -eq 0
}

$Registry = Get-Registry $Registry
if (-not $Registry) { throw 'Nothing to push to: set DOCKER_REGISTRY or pass -Registry.' }

$repo = "$Registry/$ImageName"
# Accept "1.0.0,latest" as one string too (e.g. when invoked via powershell -File).
$Tag = @($Tag | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if (-not $Tag) { $Tag = @((Get-ProjectVersion), 'latest') }

foreach ($t in $Tag) {
    $ref = "${repo}:$t"
    if (-not (& docker images -q $ref)) {
        throw "Image $ref not found locally. Build it first: .\build.ps1 -Tag $t -Registry $Registry"
    }
    if ($t -ne 'latest' -and -not $Force -and (Test-RemoteTag $ref)) {
        throw "$ref is already in the registry. Bump the version in pyproject.toml, or use -Force."
    }
}

foreach ($t in $Tag) {
    $ref = "${repo}:$t"
    Write-Host "Pushing $ref" -ForegroundColor Cyan
    # As in build.ps1: progress on stderr must not count as a failure, the exit code does.
    $ErrorActionPreference = 'Continue'
    & docker push $ref
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = 'Stop'
    if ($exitCode -ne 0) { throw "docker push $ref failed with exit code $exitCode" }
}

Write-Host 'Pushed:' -ForegroundColor Green
$Tag | ForEach-Object { Write-Host "  ${repo}:$_" }
