@echo off
rem Double-click to open the Vaani terminal UI (or run with args from a shell).
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\vaani.exe" (
  "%~dp0.venv\Scripts\vaani.exe" %*
) else (
  py -3 -m vaani %*
)
if errorlevel 1 pause
