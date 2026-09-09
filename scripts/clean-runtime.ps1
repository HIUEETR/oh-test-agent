<#
.SYNOPSIS
按显式选择清理项目内生成的运行产物。
.DESCRIPTION
所有删除路径先解析为绝对路径，并必须位于项目根目录下；Run ID 只允许安全的目录叶名称。
运行目录可按 ID、最新预检或最新真实模型成功记录保留，artifacts\phase1 始终保留。
脚本启用 SupportsShouldProcess，因此 -WhatIf 可只展示操作，实际删除会遵循 -Confirm 与确认级别。
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'High')]
param(
    [switch]$Runs,
    [string[]]$KeepRunId = @(),
    [switch]$KeepLatestPreflight,
    [switch]$KeepLatestSuccessfulRun,
    [switch]$Preflight,
    [switch]$Validation,
    [switch]$WebAcceptanceLogs,
    [switch]$Database,
    [switch]$ToolCaches,
    [switch]$AllGenerated
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$RemovedCount = 0

if ($AllGenerated) {
    $Runs = $true
    $Preflight = $true
    $Validation = $true
    $WebAcceptanceLogs = $true
    $Database = $true
    $ToolCaches = $true
}

if (($KeepRunId.Count -gt 0 -or $KeepLatestPreflight -or $KeepLatestSuccessfulRun) -and -not $Runs) {
    throw '-KeepRunId, -KeepLatestPreflight, and -KeepLatestSuccessfulRun require -Runs.'
}

if (-not ($Runs -or $Preflight -or $Validation -or $WebAcceptanceLogs -or $Database -or $ToolCaches)) {
    throw 'Select at least one cleanup target or use -AllGenerated.'
}

function Assert-ProjectPath {
    param([Parameter(Mandatory)][string]$Path)

    # 删除前统一解析路径，防止相对路径或上级目录跳出项目根目录。
    $absolute = [System.IO.Path]::GetFullPath($Path)
    $prefix = $Root.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $absolute.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside project root: $absolute"
    }
    return $absolute
}

function Assert-LeafName {
    param([Parameter(Mandatory)][string]$Name)

    if ($Name -notmatch '^[A-Za-z0-9._-]+$' -or $Name -in '.', '..') {
        throw "Run ID must be a directory leaf name without path separators: $Name"
    }
    return $Name
}

function Remove-GeneratedPath {
    param([Parameter(Mandatory)][string]$Path)

    $absolute = Assert-ProjectPath -Path $Path
    # ShouldProcess 统一承接 -WhatIf 与 -Confirm，检查通过后才执行递归删除。
    if ((Test-Path -LiteralPath $absolute) -and $PSCmdlet.ShouldProcess($absolute, 'Remove generated runtime content')) {
        Remove-Item -LiteralPath $absolute -Recurse -Force
        $script:RemovedCount++
    }
}

function Get-LatestSuccessfulRunName {
    param([Parameter(Mandatory)][string]$RunsPath)

    foreach ($directory in Get-ChildItem -LiteralPath $RunsPath -Directory -Filter 'run-*' |
        Sort-Object LastWriteTimeUtc -Descending) {
        $tracePath = Join-Path $directory.FullName 'trace.json'
        if (-not (Test-Path -LiteralPath $tracePath -PathType Leaf)) {
            continue
        }
        try {
            $trace = Get-Content -LiteralPath $tracePath -Raw | ConvertFrom-Json
        }
        catch {
            Write-Warning "Skipping unreadable trace: $tracePath"
            continue
        }
        if ($trace.state -eq 'completed' -and $trace.model_mock -eq $false) {
            return $directory.Name
        }
    }
    return $null
}

function Remove-RunDirectories {
    $runsPath = Assert-ProjectPath -Path (Join-Path $Root 'artifacts\runs')
    if (-not (Test-Path -LiteralPath $runsPath -PathType Container)) {
        return
    }

    # 保留集合使用不区分大小写的比较，与 Windows 目录名称语义一致。
    $keep = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    foreach ($runId in $KeepRunId) {
        $name = Assert-LeafName -Name $runId
        $candidate = Join-Path $runsPath $name
        if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
            throw "Requested keep Run does not exist: $name"
        }
        [void]$keep.Add($name)
    }

    if ($KeepLatestPreflight) {
        $latestPreflight = Get-ChildItem -LiteralPath $runsPath -Directory -Filter 'preflight-*' |
            Sort-Object LastWriteTimeUtc -Descending |
            Select-Object -First 1
        if (-not $latestPreflight) {
            throw 'No preflight Run exists to preserve.'
        }
        [void]$keep.Add($latestPreflight.Name)
    }

    if ($KeepLatestSuccessfulRun) {
        $latestSuccessful = Get-LatestSuccessfulRunName -RunsPath $runsPath
        if (-not $latestSuccessful) {
            throw 'No completed real-model Run exists to preserve.'
        }
        [void]$keep.Add($latestSuccessful)
    }

    if ($keep.Count -eq 0) {
        Remove-GeneratedPath -Path $runsPath
        return
    }

    Write-Host "Preserving run directories: $([string]::Join(', ', [string[]]$keep))"
    foreach ($directory in Get-ChildItem -LiteralPath $runsPath -Directory) {
        if (-not $keep.Contains($directory.Name)) {
            Remove-GeneratedPath -Path $directory.FullName
        }
    }
}

function Remove-WebAcceptanceLogs {
    $webEvidencePath = Assert-ProjectPath -Path (Join-Path $Root 'artifacts\web-acceptance')
    if (-not (Test-Path -LiteralPath $webEvidencePath -PathType Container)) {
        return
    }
    foreach ($file in Get-ChildItem -LiteralPath $webEvidencePath -File) {
        if ($file.Extension -eq '.log' -or $file.Name -eq 'pids.json') {
            Remove-GeneratedPath -Path $file.FullName
        }
    }
}

if ($Runs) { Remove-RunDirectories }
if ($Preflight) { Remove-GeneratedPath (Join-Path $Root 'artifacts\preflight') }
if ($Validation) { Remove-GeneratedPath (Join-Path $Root 'artifacts\validation') }
if ($WebAcceptanceLogs) { Remove-WebAcceptanceLogs }
if ($Database) {
    Get-ChildItem -LiteralPath (Join-Path $Root 'artifacts') -File -Filter 'agent.db*' -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-GeneratedPath $_.FullName }
}
# phase1 历史验收证据不属于任何清理目标，调用 -AllGenerated 时也会保留。
if ($ToolCaches) {
    Remove-GeneratedPath (Join-Path $Root '.pytest_cache')
    Remove-GeneratedPath (Join-Path $Root '.ruff_cache')
    Remove-GeneratedPath (Join-Path $Root 'web\dist')
    Remove-GeneratedPath (Join-Path $Root 'web\.vite')
}

Write-Host "Selected runtime cleanup completed. Removed paths: $RemovedCount. artifacts\phase1 was preserved."
