@echo off
title AI Web Spider Setup
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (echo Install Python 3.11+ from https://www.python.org/downloads/ and enable Add Python to PATH. & pause & exit /b 1)
py -3 -m venv .venv
if errorlevel 1 (echo Failed to create virtual environment.& pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt
if errorlevel 1 (echo Package installation failed.& pause & exit /b 1)
echo Setup complete. Install Ollama from https://ollama.com/download
echo Then run: ollama pull llama3.2
echo Then run: ollama pull nomic-embed-text
echo Finally double-click launch.bat
pause
