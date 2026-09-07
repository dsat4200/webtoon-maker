param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$pythonExecutable = & $Python -c 'import sys, PySide6, PIL, numpy, scipy; print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw "Install requirements.txt in the selected Python environment first." }
$pythonWindowed = Join-Path (Split-Path $pythonExecutable.Trim()) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonWindowed)) { throw "pythonw.exe was not found beside Python." }
$compiler = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $compiler)) { throw "The Windows .NET Framework C# compiler was not found." }
$output = Join-Path $PSScriptRoot "WebtoonMaker.exe"
& $compiler /nologo /target:winexe /reference:System.Windows.Forms.dll "/out:$output" (Join-Path $PSScriptRoot "launcher\WebtoonMaker.cs")
if ($LASTEXITCODE -ne 0) { throw "Launcher compilation failed." }
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "WebtoonMaker.python.txt"), $pythonWindowed)
Write-Output "Built $output"
Write-Output "Choose this executable in Blender Preferences > File Paths > Applications > Image Editor."
