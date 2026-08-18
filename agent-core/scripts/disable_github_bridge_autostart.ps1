$Startup = [Environment]::GetFolderPath("Startup")
$Cmd = Join-Path $Startup "AjanOrdusuGitHubBridge.cmd"
if (Test-Path $Cmd) { Remove-Item -Force $Cmd }
Write-Host "Ajan Ordusu GitHub Bridge otomatik baslatma kapatildi."
