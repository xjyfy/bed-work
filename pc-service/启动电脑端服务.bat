@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem Start the service with a hidden console window (python.exe, no visible window).
wscript.exe "%~dp0start-hidden.vbs"
exit
