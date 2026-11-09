@echo off
setlocal enabledelayedexpansion

title iPhone Revival - Windows Assistant
echo ======================================================================
echo                  IPHONE REVIVAL ASSISTANT (WINDOWS)
echo ======================================================================
echo.

REM 1. Check Python
where python >nul 2>&1
if %errorlevel% neq 0 (
    where py >nul 2>&1
    if %errorlevel% neq 0 (
        echo [!] Python 3 is not detected in your PATH.
        echo     Please install Python 3 from https://www.python.org or the Microsoft Store.
        echo.
        pause
        exit /b 1
    )
    set PY_CMD=py
) else (
    set PY_CMD=python
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

REM 3. Run Master Revival Control Plane with Windows-MCP Autopilot
%PY_CMD% iphone_manager.py revive --auto-drive %*
set REVIVE_EXIT=%errorlevel%

if %REVIVE_EXIT% equ 2 (
    echo.
    echo ----------------------------------------------------------------------
    echo [!] Official iTunes is required to communicate with your iPhone on Windows.
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
