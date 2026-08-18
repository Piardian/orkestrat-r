@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo Ajan Ordusu - Main Branch Koruma
echo ==========================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\protect_main_branch.ps1"
if errorlevel 1 goto :fail
echo.
echo Koruma basariyla uygulandi.
pause
exit /b 0

:fail
echo.
echo Koruma uygulanamadi. Yukaridaki hatayi bana gonderin.
pause
exit /b 1
