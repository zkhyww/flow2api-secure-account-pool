@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if exist "%~dp0venv\Scripts\pythonw.exe" (
  start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0start-flow2api.pyw"
  exit /b 0
)
where pyw.exe >nul 2>nul
if not errorlevel 1 (
  start "" pyw.exe "%~dp0start-flow2api.pyw"
  exit /b 0
)
where pythonw.exe >nul 2>nul
if not errorlevel 1 (
  start "" pythonw.exe "%~dp0start-flow2api.pyw"
  exit /b 0
)
where py.exe >nul 2>nul
if not errorlevel 1 (
  start "" py.exe "%~dp0start-flow2api.pyw"
  exit /b 0
)
where python.exe >nul 2>nul
if not errorlevel 1 (
  start "" python.exe "%~dp0start-flow2api.pyw"
  exit /b 0
)
echo Flow2API requires Python 3.11 or newer. Install it, then run this file again.
exit /b 1
