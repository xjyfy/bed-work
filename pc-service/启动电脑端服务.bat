@echo off
cd /d "%~dp0"

rem Launch the service via a hidden WScript host.
rem WScript waits for python and shows a message box if startup fails.
start "" wscript.exe "%~dp0start-hidden.vbs"
exit
