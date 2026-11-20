@echo off
cd /d "%~dp0"

rem Prepare runtime env: create repo-root .venv and install pc-service deps.
rem Uses uv when available (auto-downloads CPython 3.12), else python -m venv + pip.

set "VENV=%~dp0..\.venv"
set "PY=%VENV%\Scripts\python.exe"

if exist "%PY%" goto install

where uv >nul 2>nul
if errorlevel 1 goto nopython

echo Creating %VENV% with uv (CPython 3.12)...
uv venv --python 3.12 "%VENV%"
if errorlevel 1 goto fail
goto install

:nopython
echo [ERROR] Neither %VENV% nor uv is available.
echo Install Python 3.12 (with "Add to PATH") or uv, then retry.
pause
exit /b 1

:install
echo Installing dependencies into "%PY%" ...
where uv >nul 2>nul
if errorlevel 1 goto pipinstall

uv pip install --python "%PY%" -r requirements.txt
if errorlevel 1 goto fail
goto done

:pipinstall
"%PY%" -m pip install -r requirements.txt
if errorlevel 1 goto fail

:done
echo.
echo Done. You can now run "启动电脑端服务.bat".
pause
exit /b 0

:fail
echo.
echo [FAILED] Dependency installation failed. See errors above.
pause
exit /b 1
