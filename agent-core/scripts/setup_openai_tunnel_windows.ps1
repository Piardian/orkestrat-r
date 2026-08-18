$ErrorActionPreference = "Stop"
$CoreDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$SecretDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\secrets"
$ConfigDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\config"
$ToolsDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\tools"
New-Item -ItemType Directory -Force -Path $SecretDir, $ConfigDir, $ToolsDir | Out-Null

if (-not (Test-Path (Join-Path $SecretDir "github_mcp_token.xml"))) {
    throw "Once setup_github_mcp_windows.ps1 calistirilmali."
}

Write-Host "=== Ajan Ordusu - OpenAI Secure MCP Tunnel kurulumu ===" -ForegroundColor Cyan
Write-Host "Bu adim OpenAI Platform'da tunnel_id ve runtime API key gerektirir." -ForegroundColor Yellow
Start-Process "https://platform.openai.com/settings/organization/tunnels"
Start-Process "https://platform.openai.com/settings/organization/api-keys"

$TunnelId = Read-Host "Platform Tunnels sayfasindaki tunnel_id degerini yapistir (tunnel_...)"
if ($TunnelId -notmatch '^tunnel_[0-9a-f]{32}$') {
    throw "tunnel_id formati gecersiz. Beklenen: tunnel_ + 32 kucuk hex karakter."
}
$SecureOpenAIKey = Read-Host "Tunnel-client icin OpenAI runtime API key'i yapistir" -AsSecureString
$SecureOpenAIKey | Export-Clixml -Path (Join-Path $SecretDir "openai_tunnel_key.xml")
@{ tunnel_id = $TunnelId; health_port = 8877 } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $ConfigDir "openai_tunnel.json")

Write-Host "Guncel resmi tunnel-client indiriliyor..."
$Release = Invoke-RestMethod -Uri "https://api.github.com/repos/openai/tunnel-client/releases/latest" -Headers @{"User-Agent"="ajan-ordusu-setup"}
$Arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
$Needle = if ($Arch -eq "arm64") { "windows-arm64.zip" } else { "windows-amd64.zip" }
$Asset = $Release.assets | Where-Object { $_.name -like "*$Needle" } | Select-Object -First 1
if (-not $Asset) { throw "Tunnel-client Windows paketi son release icinde bulunamadi ($Needle)." }

$ZipPath = Join-Path $ToolsDir "tunnel-client.zip"
$ExtractDir = Join-Path $ToolsDir "tunnel-client-extracted"
Invoke-WebRequest -Uri $Asset.browser_download_url -OutFile $ZipPath
if (Test-Path $ExtractDir) { Remove-Item -Recurse -Force $ExtractDir }
Expand-Archive -Path $ZipPath -DestinationPath $ExtractDir -Force
$Exe = Get-ChildItem -Path $ExtractDir -Recurse -Filter "tunnel-client.exe" | Select-Object -First 1
if (-not $Exe) { throw "tunnel-client.exe indirilen pakette bulunamadi." }
$FinalExe = Join-Path $ToolsDir "tunnel-client.exe"
Copy-Item -Force $Exe.FullName $FinalExe

Write-Host "Tunnel-client: $FinalExe" -ForegroundColor Green
& $FinalExe --version

Write-Host "Bridge ilk kez baslatiliyor..."
$StartScript = Join-Path $CoreDir "scripts\start_github_repo_bridge.ps1"
$Proc = Start-Process powershell.exe -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",('"' + $StartScript + '"')) -PassThru
Start-Sleep -Seconds 5

$McpOk = $false
try {
    $Tcp = New-Object System.Net.Sockets.TcpClient
    $Tcp.Connect("127.0.0.1",8765)
    $McpOk = $Tcp.Connected
    $Tcp.Close()
} catch {}
if (-not $McpOk) { throw "Yerel MCP 127.0.0.1:8765 uzerinde acilmadi. Loglari kontrol edin." }

$TunnelReady = $false
for ($i=0; $i -lt 20 -and -not $TunnelReady; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8877/readyz" -TimeoutSec 2
        $TunnelReady = ($r.StatusCode -eq 200)
    } catch {
        Start-Sleep -Seconds 1
    }
}
if ($TunnelReady) {
    Write-Host "GitHub MCP + Secure MCP Tunnel READY." -ForegroundColor Green
} else {
    Write-Host "GitHub MCP ayakta ama tunnel henuz READY degil. GITHUB_REPO_BRIDGE_STATUS.bat ile kontrol edin." -ForegroundColor Yellow
}
Write-Host "Son adim ChatGPT web: Settings/Workspace Settings > Apps > Create > Connection: Tunnel > bu tunnel'i sec > Scan Tools > Create." -ForegroundColor Yellow
Write-Host "create_repository araci gorunmeli. Yazma eylemleri ChatGPT Business/Enterprise/Edu full MCP gerektirir." -ForegroundColor Yellow
