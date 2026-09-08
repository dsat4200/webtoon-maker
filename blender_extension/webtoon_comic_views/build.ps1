param([string]$BlenderExecutable = $env:BLENDER_EXECUTABLE)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$output = Split-Path -Parent $source
$executable = $BlenderExecutable
if (-not $executable) {
    $candidates = @(
        $env:BLENDER_52_EXECUTABLE,
        "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe",
        $env:BLENDER_45_EXECUTABLE,
        "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
    )
    $blender = Get-Command blender -ErrorAction SilentlyContinue
    if ($blender) { $candidates += $blender.Source }
    $executable = $candidates | Where-Object {
        $_ -and (Test-Path -LiteralPath $_ -PathType Leaf)
    } | Select-Object -First 1
}
if (-not $executable -or -not (Test-Path -LiteralPath $executable -PathType Leaf)) {
    throw "Blender was not found. Install Blender 5.2 LTS or set -BlenderExecutable to blender.exe (4.5 LTS is also supported)."
}
& $executable --factory-startup --command extension validate $source
if ($LASTEXITCODE -ne 0) { throw "Blender extension validation failed ($LASTEXITCODE)." }
& $executable --factory-startup --command extension build --source-dir $source --output-dir $output
if ($LASTEXITCODE -ne 0) { throw "Blender extension build failed ($LASTEXITCODE)." }
