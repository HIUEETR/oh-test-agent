[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [switch]$Runs,
    [switch]$Preflight,
    [switch]$Validation,
    [switch]$Database,
    [switch]$ToolCaches,
    [switch]$AllGenerated
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

if ($AllGenerated) {
    $Runs = $true
    $Preflight = $true
    $Validation = $true
    $Database = $true
    $ToolCaches = $true
}

if (-not ($Runs -or $Preflight -or $Validation -or $Database -or $ToolCaches)) {
    throw 'Select at least one cleanup target or use -AllGenerated.'
}

function Assert-ProjectPath {
    param([Parameter(Mandatory)][string]$Path)

    $absolute = [System.IO.Path]::GetFullPath($Path)
    $prefix = $Root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $absolute.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside project root: $absolute"
    }
    return $absolute
}

function Remove-GeneratedPath {
    param([Parameter(Mandatory)][string]$Path)

    $absolute = Assert-ProjectPath -Path $Path
    if ((Test-Path -LiteralPath $absolute) -and $PSCmdlet.ShouldProcess($absolute, 'Remove generated runtime content')) {
        Remove-Item -LiteralPath $absolute -Recurse -Force
    }
}

if ($Runs) { Remove-GeneratedPath (Join-Path $Root 'artifacts\runs') }
if ($Preflight) { Remove-GeneratedPath (Join-Path $Root 'artifacts\preflight') }
if ($Validation) { Remove-GeneratedPath (Join-Path $Root 'artifacts\validation') }
if ($Database) {
    Get-ChildItem -LiteralPath (Join-Path $Root 'artifacts') -File -Filter 'agent.db*' -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-GeneratedPath $_.FullName }
}
if ($ToolCaches) {
    Remove-GeneratedPath (Join-Path $Root '.pytest_cache')
    Remove-GeneratedPath (Join-Path $Root '.ruff_cache')
    Remove-GeneratedPath (Join-Path $Root 'web\dist')
    Remove-GeneratedPath (Join-Path $Root 'web\.vite')
}

Write-Host 'Selected runtime cleanup completed. artifacts\phase1 was preserved.'
