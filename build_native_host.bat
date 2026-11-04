@echo off
setlocal
cd /d "%~dp0"

where cl >nul 2>nul
if errorlevel 1 (
  echo Microsoft C/C++ compiler was not found.
  echo Run this script from a Visual Studio Developer Command Prompt.
  exit /b 1
)

cl /nologo /O2 /W4 /DUNICODE /D_UNICODE /Fe:MortisNativeHost.exe mortis_native_launcher.c /link /SUBSYSTEM:WINDOWS shell32.lib
if errorlevel 1 (
  echo Failed to build MortisNativeHost.exe.
  exit /b 1
)

echo Built MortisNativeHost.exe. The binary is generated locally and is ignored by Git.
exit /b 0
