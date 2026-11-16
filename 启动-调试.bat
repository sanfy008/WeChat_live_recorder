@echo off
chcp 936 >nul
title 微信直播录音转写 - 调试
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if exist ".venv-trim\Scripts\python.exe" set "PY=.venv-trim\Scripts\python.exe"

echo 使用: %PY%
echo.
"%PY%" -m wxlive gui

echo.
echo 程序已退出，错误码: %errorlevel%
pause
