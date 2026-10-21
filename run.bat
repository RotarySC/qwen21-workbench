@echo off
REM ASCII ONLY. Do NOT put non-ASCII (Chinese) text in this file.
REM
REM Why: this machine's console codepage is GBK (936). cmd.exe reads a .bat
REM byte-wise and decodes it as the console codepage, so a UTF-8 file WITHOUT
REM a BOM is mis-decoded into garbage. That garbage broke the "if ( ... )"
REM block below and cmd exited BEFORE the pause, so double-clicking just
REM flashed a black window with no message.
REM Keep every line in this file ASCII, or save it as GBK.
REM
REM Usage:
REM   run.bat            start on 127.0.0.1:8189
REM   run.bat 8200       use another port
setlocal

set "REPO=%~dp0"
set "REPO=%REPO:~0,-1%"

REM Code belongs to this checkout. Operational data belongs to the primary
REM checkout's parent, including when this script runs in a linked worktree.
set "PRIMARY_REPO=%REPO%"
set "GIT_COMMON="
for /f "delims=" %%G in ('git -C "%REPO%" rev-parse --path-format^=absolute --git-common-dir 2^>nul') do set "GIT_COMMON=%%G"
if not defined GIT_COMMON goto :workspace
for %%G in ("%GIT_COMMON%\..") do set "PRIMARY_REPO=%%~fG"

:workspace
set "WORKSPACE=%QWEN21_WORKSPACE%"
if not defined WORKSPACE for %%G in ("%PRIMARY_REPO%\..") do set "WORKSPACE=%%~fG"
set "QWEN21_WORKSPACE=%WORKSPACE%"
if not defined QWEN21_STATE set "QWEN21_STATE=%PRIMARY_REPO%\state"
REM Interpreter, in order of preference:
REM   1. QWEN21_PYTHON             (explicit override)
REM   2. %LOCALAPPDATA%\qwen21-venv  <- the supported one. It lives OUTSIDE the
REM      project tree because the DSH sandbox stamps every file created under
REM      this tree with a Low integrity label, and a process launched from a
REM      Low file can write neither %TEMP% nor the ComfyUI install dir -- it
REM      would start the backend in "confined mode" (see AGENTS.md, rule 2).
REM   3. <workspace>\.venv         (legacy, inside the tree, Low -> confined)
set "PY=%QWEN21_PYTHON%"
if not defined PY set "PY=%LOCALAPPDATA%\qwen21-venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%WORKSPACE%\.venv\Scripts\python.exe"

if not exist "%PY%" goto :nopython
if "%~1"=="" (set "PORT=8189") else (set "PORT=%~1")

echo.
echo   Workbench : http://127.0.0.1:%PORT%
echo   Backend   : http://127.0.0.1:8188   (start it with the switch in the
echo               page's top-right corner; the exact command is in
echo               Log -^> Environment, ready to copy)
echo.
echo   Source    : %REPO%
echo   Workspace : %WORKSPACE%
echo   State     : %QWEN21_STATE%
echo   Python    : %PY%
echo.
echo   Closing this window stops the workbench. ComfyUI is NOT affected.
echo.

cd /d "%REPO%"
"%PY%" -m workbench.server --port %PORT%

echo.
echo   Workbench exited.
pause
exit /b 0

:nopython
echo.
echo   [x] Python not found: %PY%
echo       Create the venv OUTSIDE this project tree (see AGENTS.md, rule 2):
echo         "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" -m venv "%LOCALAPPDATA%\qwen21-venv"
echo         "%LOCALAPPDATA%\qwen21-venv\Scripts\python.exe" -m pip install -r "%~dp0requirements-lock.txt"
echo       or set QWEN21_PYTHON to an installed interpreter with the dependencies.
echo.
pause
exit /b 1
