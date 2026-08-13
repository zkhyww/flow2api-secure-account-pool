@echo off
setlocal EnableExtensions
cd /d "%~dp0"
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0start-flow2api.pyw"
exit /b 0
