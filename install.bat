@echo off
REM ==============================================================================
REM Multi-Board PCB Manager - Windows Installation
REM ==============================================================================
REM Automatically detects KiCad version and installs to the correct directory.
REM
REM Usage:
REM   Double-click install.bat
REM   or run from command prompt: install.bat
REM
REM Author: Eliot Abramo
REM License: MIT
REM ==============================================================================

setlocal EnableDelayedExpansion

set "PLUGIN_NAME=multi_pcb_manager_v2"
set "MIN_KICAD_VERSION=9.0"

REM Get script directory
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo.
echo ==============================================================================
echo   Multi-Board PCB Manager - Installation
echo ==============================================================================
echo.

REM ==============================================================================
REM Detect KiCad Version
REM ==============================================================================

set "KICAD_VERSION="

REM Check for KiCad 9.0 first (preferred)
if exist "%APPDATA%\kicad\9.0" (
    set "KICAD_VERSION=9.0"
    goto :version_found
)

REM Fall back to 8.0
if exist "%APPDATA%\kicad\8.0" (
    set "KICAD_VERSION=8.0"
    goto :version_found
)

REM Fall back to 7.0
if exist "%APPDATA%\kicad\7.0" (
    set "KICAD_VERSION=7.0"
    goto :version_found
)

REM No KiCad found, default to 9.0
set "KICAD_VERSION=9.0"
echo [!] No existing KiCad config found, defaulting to version 9.0

:version_found
echo [+] Target KiCad version: %KICAD_VERSION%

REM ==============================================================================
REM Set Plugin Directory
REM ==============================================================================

set "PLUGIN_DIR=%APPDATA%\kicad\%KICAD_VERSION%\scripting\plugins"
echo [i] Plugin directory: %PLUGIN_DIR%
echo.

REM ==============================================================================
REM Check for kicad-cli
REM ==============================================================================

where kicad-cli >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [+] kicad-cli found in PATH
) else (
    echo [!] kicad-cli not found in PATH
    echo [i] The plugin requires kicad-cli for netlist export
    echo [i] Make sure KiCad is fully installed
)
echo.

REM ==============================================================================
REM Create Directory
REM ==============================================================================

if not exist "%PLUGIN_DIR%" (
    echo [i] Creating plugin directory...
    mkdir "%PLUGIN_DIR%"
    if !ERRORLEVEL! NEQ 0 (
        echo [X] Failed to create directory
        goto :error
    )
)

REM ==============================================================================
REM Remove Old Installation
REM ==============================================================================

if exist "%PLUGIN_DIR%\%PLUGIN_NAME%" (
    echo [i] Removing previous installation...
    rmdir /s /q "%PLUGIN_DIR%\%PLUGIN_NAME%"
    if !ERRORLEVEL! NEQ 0 (
        echo [!] Warning: Could not fully remove old installation
    )
)

REM ==============================================================================
REM Copy Plugin Files
REM ==============================================================================

echo [i] Installing plugin files...
xcopy /s /e /i /q "%SCRIPT_DIR%" "%PLUGIN_DIR%\%PLUGIN_NAME%" >nul
if %ERRORLEVEL% NEQ 0 (
    echo [X] Failed to copy plugin files
    goto :error
)

REM Remove install scripts from installed copy
del /q "%PLUGIN_DIR%\%PLUGIN_NAME%\install.bat" >nul 2>&1
del /q "%PLUGIN_DIR%\%PLUGIN_NAME%\install.sh" >nul 2>&1

echo [+] Plugin installed to: %PLUGIN_DIR%\%PLUGIN_NAME%

REM ==============================================================================
REM Success
REM ==============================================================================

echo.
echo ==============================================================================
echo   [+] Installation Complete!
echo ==============================================================================
echo.
echo Next steps:
echo   1. Open KiCad PCB Editor
echo   2. Go to: Tools -^> External Plugins -^> Refresh Plugins
echo   3. Access: Tools -^> External Plugins -^> Multi-Board Manager
echo.
echo Documentation: https://github.com/yourusername/multi-pcb-manager
echo.
goto :end

:error
echo.
echo ==============================================================================
echo   [X] Installation Failed
echo ==============================================================================
echo.
echo Please try:
echo   - Running as Administrator
echo   - Manually copying the plugin folder to:
echo     %PLUGIN_DIR%\%PLUGIN_NAME%
echo.

:end
pause
