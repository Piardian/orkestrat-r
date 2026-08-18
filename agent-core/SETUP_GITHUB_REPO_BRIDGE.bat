@echo off
setlocal
cd /d "%~dp0"
echo ==========================================
echo Ajan Ordusu - GitHub Repo Bridge Kurulumu
echo ==========================================
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup_github_mcp_windows.ps1"
if errorlevel 1 goto :fail
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup_openai_tunnel_windows.ps1"
if errorlevel 1 goto :fail
echo.
echo Kurulum tamamlandi.
echo ChatGPT web'de Apps ^> Create ^> Tunnel ile son baglantiyi yapin.
pause
exit /b 0
:fail
echo.
echo Kurulum bir hata nedeniyle durdu. Yukaridaki mesaji bana gonderin.
pause
exit /b 1
