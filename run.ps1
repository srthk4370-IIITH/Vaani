# Launch Vaani from the local venv. Usage: .\run.ps1 [args]
& "$PSScriptRoot\.venv\Scripts\vaani.exe" @args
exit $LASTEXITCODE
