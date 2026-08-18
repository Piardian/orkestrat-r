$ErrorActionPreference = "Stop"
$CoreDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$SecretDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\secrets"
$ConfigDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\config"
$TokenPath = Join-Path $SecretDir "github_mcp_token.xml"
$ConfigPath = Join-Path $ConfigDir "github_mcp.json"
$VenvPython = Join-Path $CoreDir ".venv-mcp\Scripts\python.exe"
if (-not (Test-Path $TokenPath)) { throw "GitHub token ayarlanmamis." }
if (-not (Test-Path $ConfigPath)) { throw "GitHub MCP config bulunamadi." }
if (-not (Test-Path $VenvPython)) { throw ".venv-mcp bulunamadi." }

$Secure = Import-Clixml $TokenPath
$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
try { $env:GITHUB_MCP_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr) }
finally { if ($Bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) } }
$Cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$env:GITHUB_MCP_ALLOWED_OWNER = [string]$Cfg.allowed_owner
$env:GITHUB_MCP_ALLOW_CREATE = if ($Cfg.allow_create) { "true" } else { "false" }
$env:GITHUB_MCP_ALLOW_PUBLIC = if ($Cfg.allow_public) { "true" } else { "false" }
$env:GITHUB_MCP_HOST = [string]$Cfg.host
$env:GITHUB_MCP_PORT = [string]$Cfg.port
Set-Location $CoreDir
& $VenvPython (Join-Path $CoreDir "run_github_mcp.py")
