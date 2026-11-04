@echo off
title MORTIS - AI Web Spider
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first.& exit /b 1)
"%~dp0.venv\Scripts\python.exe" "%~dp0launch_mortis.py"
exit /b %ERRORLEVEL%
