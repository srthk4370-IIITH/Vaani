# Vaani installer for Windows (PowerShell)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $py = @("py", "-3") }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = @("python") }
else { Write-Error "Python 3.10+ not found. Install from https://python.org (tick 'Add to PATH')."; exit 1 }

$exe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1)
& $exe @pyArgs -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if ($LASTEXITCODE -ne 0) { Write-Error "Python 3.10+ required."; exit 1 }

if (-not (Test-Path ".venv")) { & $exe @pyArgs -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\pip.exe install -e ".[windows]"
if ($LASTEXITCODE -ne 0) { Write-Error "pip install failed."; exit 1 }

Write-Host "Installed. Running onboarding..."
& .\.venv\Scripts\vaani.exe setup
Write-Host "Done. Start Vaani with: .\vaani.cmd  (or .\run.ps1)"
