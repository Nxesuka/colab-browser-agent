@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_and_run.ps1" %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERREUR] L'agent s'est termine avec le code %ERRORLEVEL%.
    pause
)
