@echo off
chcp 936 >nul
title WeChat Live Recorder
cd /d "%~dp0"

set "PYW=.venv\Scripts\pythonw.exe"
if exist ".venv-trim\Scripts\pythonw.exe" set "PYW=.venv-trim\Scripts\pythonw.exe"

if not exist "%PYW%" (
    echo [ERROR] Python env not found.
    pause
    exit /b 1
)

start "" "%PYW%" -m wxlive gui
exit /b 0
