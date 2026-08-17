$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$output = Split-Path -Parent $source
$blender = Get-Command blender -ErrorAction SilentlyContinue
if (-not $blender) {
    $candidate = "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
    if (-not (Test-Path -LiteralPath $candidate)) {
        throw "Blender 4.5 was not found. Add blender.exe to PATH."
    }
    $executable = $candidate
} else {
    $executable = $blender.Source
}
& $executable --factory-startup --command extension validate $source
& $executable --factory-startup --command extension build --source-dir $source --output-dir $output
