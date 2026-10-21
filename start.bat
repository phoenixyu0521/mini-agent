@echo off
rem ============================================================
rem  mini-agent launcher  (entry point for the desktop shortcut)
rem
rem  THIS FILE IS PURE ASCII ON PURPOSE.
rem  cmd.exe parses .bat byte by byte using the OEM code page
rem  (936 on Chinese Windows). UTF-8 Chinese text inside a .bat
rem  does not just show up as mojibake -- it breaks command
rem  parsing, because multi-byte characters contain bytes that
rem  cmd treats as control characters. All human-facing Chinese
rem  output lives in launcher.py instead.
rem
rem  All paths are derived from %~dp0 (this file's folder), so the
rem  directory can be moved or copied to another machine and the
rem  shortcut keeps working.
rem ============================================================

setlocal
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto run

echo.
echo   [.] No .venv found. Preparing the Python environment...
where uv >nul 2>&1
if errorlevel 1 goto no_uv

call uv sync
if errorlevel 1 goto sync_failed
if not exist "%VENV_PY%" goto sync_failed

:run
"%VENV_PY%" "%~dp0launcher.py"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%

:no_uv
echo.
echo   [x] uv is not installed, so the environment cannot be created.
echo       1^) Install uv:  https://docs.astral.sh/uv/
echo       2^) Run in this folder:  uv sync
echo       3^) Double-click this file again
echo.
pause
exit /b 1

:sync_failed
echo.
echo   [x] "uv sync" failed. Scroll up for the actual error.
echo.
pause
exit /b 1
