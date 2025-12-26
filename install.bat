@echo off
REM Multi-Board PCB Manager - Windows Installation
REM Installs to KiCad 8.0 plugins directory

setlocal

set PLUGIN_NAME=multi_pcb_manager_v2
set KICAD_VERSION=8.0

echo ================================================
echo Multi-Board PCB Manager - Installation
echo ================================================

REM Try to find KiCad plugins directory
if exist "%APPDATA%\kicad\9.0" (
    set KICAD_VERSION=9.0
) else if exist "%APPDATA%\kicad\8.0" (
    set KICAD_VERSION=8.0
) else if exist "%APPDATA%\kicad\7.0" (
    set KICAD_VERSION=7.0
)

set PLUGIN_DIR=%APPDATA%\kicad\%KICAD_VERSION%\scripting\plugins

echo Target KiCad version: %KICAD_VERSION%
echo Plugin directory: %PLUGIN_DIR%

REM Create directory if needed
if not exist "%PLUGIN_DIR%" mkdir "%PLUGIN_DIR%"

REM Remove old installation
if exist "%PLUGIN_DIR%\%PLUGIN_NAME%" (
    echo Removing previous installation...
    rmdir /s /q "%PLUGIN_DIR%\%PLUGIN_NAME%"
)

REM Copy plugin files
echo Copying plugin files...
xcopy /s /e /i /q "%~dp0" "%PLUGIN_DIR%\%PLUGIN_NAME%"

echo.
echo ================================================
echo Installation complete!
echo ================================================
echo.
echo Next steps:
echo   1. Open KiCad PCB Editor
echo   2. Tools -^> External Plugins -^> Refresh Plugins
echo   3. Tools -^> External Plugins -^> Multi-Board Manager
echo.
pause
