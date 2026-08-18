$ErrorActionPreference = "Stop"
$CoreDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$SecretDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\secrets"
$ConfigDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\config"
$ToolsDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\tools"
$LogDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-Port([int]$Port) {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $Port)
        $ok = $c.Connected
        $c.Close()
        return $ok
    } catch { return $false }
}

if (-not (Test-Port 8765)) {
    $McpScript = Join-Path $CoreDir "scripts\start_github_mcp.ps1"
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",('"' + $McpScript + '"')) -RedirectStandardOutput (Join-Path $LogDir "github-mcp.out.log") -RedirectStandardError (Join-Path $LogDir "github-mcp.err.log") | Out-Null
    for ($i=0; $i -lt 20 -and -not (Test-Port 8765); $i++) { Start-Sleep -Milliseconds 500 }
}
if (-not (Test-Port 8765)) { throw "GitHub MCP baslatilamadi." }

$KeyPath = Join-Path $SecretDir "openai_tunnel_key.xml"
$TunnelCfgPath = Join-Path $ConfigDir "openai_tunnel.json"
$TunnelExe = Join-Path $ToolsDir "tunnel-client.exe"
if (-not (Test-Path $KeyPath)) { throw "OpenAI tunnel runtime key ayarlanmamis." }
if (-not (Test-Path $TunnelCfgPath)) { throw "OpenAI tunnel config bulunamadi." }
if (-not (Test-Path $TunnelExe)) { throw "tunnel-client.exe bulunamadi." }

$Secure = Import-Clixml $KeyPath
$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
try { $env:CONTROL_PLANE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr) }
finally { if ($Bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) } }
$Cfg = Get-Content $TunnelCfgPath -Raw | ConvertFrom-Json
$TunnelId = [string]$Cfg.tunnel_id
$Health = "127.0.0.1:$($Cfg.health_port)"

& $TunnelExe run `
    --control-plane.tunnel-id $TunnelId `
    --mcp.server-url "http://127.0.0.1:8765/mcp" `
    --health.listen-addr $Health `
    --log.level info `
    --log.format struct-text
