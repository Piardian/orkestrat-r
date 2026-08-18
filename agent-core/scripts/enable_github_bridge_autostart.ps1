$ErrorActionPreference = "Stop"
$CoreDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$StartScript = Join-Path $CoreDir "scripts\start_github_repo_bridge.ps1"
$Startup = [Environment]::GetFolderPath("Startup")
$Cmd = Join-Path $Startup "AjanOrdusuGitHubBridge.cmd"
$Line = '@echo off' + "`r`n" + 'start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $StartScript + '"' + "`r`n"
Set-Content -Path $Cmd -Value $Line -Encoding ASCII
Write-Host "Otomatik baslatma etkin: $Cmd" -ForegroundColor Green
Write-Host "Windows oturumu acildiginda GitHub MCP + Secure MCP Tunnel baslatilacak."
