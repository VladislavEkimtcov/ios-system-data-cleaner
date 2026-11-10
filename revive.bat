@echo off
setlocal enabledelayedexpansion

title iPhone Revival - Windows Assistant
echo ======================================================================
echo                  IPHONE REVIVAL ASSISTANT (WINDOWS)
echo ======================================================================
echo.

REM Run from the script's own folder, whatever the caller's current directory is.
cd /d "%~dp0"

REM UTF-8 console so the pipeline's emoji / box-drawing output does not crash on cp437/cp1252.
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

REM 1. Find a Python 3.10+ that actually runs.
REM    `where python` is not enough: it also matches the Microsoft Store "App Execution
REM    Alias" stub, which opens the Store instead of running Python. So run each candidate.
REM    `if errorlevel 1` is read at run time (unlike %%errorlevel%% inside a ( ) block).
set "PY_CMD="
set "PY_PROBE=import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
py -3 -c "%PY_PROBE%" >nul 2>&1
if not errorlevel 1 set "PY_CMD=py -3"
if not defined PY_CMD (
    python -c "%PY_PROBE%" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=python"
)
if not defined PY_CMD (
    echo [!] Python 3.10 or newer was not found.
    echo     Install it from https://www.python.org ^(tick "Add python.exe to PATH"^)
    echo     or from the Microsoft Store, then run revive.bat again.
    echo.
    pause
    exit /b 1
)

REM CI / self-check: prove the Python probe above works, then stop (no phone, no pause).
if defined REVIVE_PROBE_ONLY (
    echo PYTHON_OK: %PY_CMD%
    exit /b 0
)

REM 2. Detect System Architecture (64-bit vs 32-bit)
if defined PROCESSOR_ARCHITEW6432 (
    set ITUNES_URL=https://www.apple.com/itunes/download/win64
    set ARCH_BITS=64-bit
) else if "%PROCESSOR_ARCHITECTURE%"=="AMD64" (
    set ITUNES_URL=https://www.apple.com/itunes/download/win64
    set ARCH_BITS=64-bit
) else if "%PROCESSOR_ARCHITECTURE%"=="ARM64" (
    set ITUNES_URL=https://www.apple.com/itunes/download/win64
    set ARCH_BITS=64-bit
) else (
    set ITUNES_URL=https://www.apple.com/itunes/download/win32
    set ARCH_BITS=32-bit
)

REM 3. The revival pipeline: new backup, verification, safety copy, then the erase instructions.
REM    Exit 2 = the backup is safe but this PC has nothing to restore with (iTunes missing): don't erase yet.
%PY_CMD% iphone_manager.py revive %*
set REVIVE_EXIT=%errorlevel%

if %REVIVE_EXIT% equ 2 (
    echo.
    echo ----------------------------------------------------------------------
    echo [!] Your backup is safe, but do NOT erase the phone yet: iTunes is needed to put it back.
    echo     Detected Architecture: %ARCH_BITS%
    echo     Direct Apple Link:     !ITUNES_URL!
    echo.
    set /p OPEN_APPLE="Would you like to open the official Apple iTunes download link? [Y/n]: "
    if /i "!OPEN_APPLE!"=="" set OPEN_APPLE=y
    if /i "!OPEN_APPLE!"=="y" (
        start !ITUNES_URL!
        echo.
        echo [*] Opened !ITUNES_URL! in your web browser.
        echo     Please run the downloaded installer, complete setup,
        echo     and then run revive.bat again.
    )
)

echo.
echo ======================================================================
echo Session finished. Press any key to exit.
echo ======================================================================
pause >nul

REM Forward the pipeline's exit code to the caller (CI / scripts rely on it).
exit /b %REVIVE_EXIT%
