param(
    [string]$ExpectedOwner = "Piardian"
)

$ErrorActionPreference = "Stop"
$CoreDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$SecretDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\secrets"
$ConfigDir = Join-Path $env:LOCALAPPDATA "AjanOrdusu\config"
New-Item -ItemType Directory -Force -Path $SecretDir, $ConfigDir | Out-Null

Write-Host "=== Ajan Ordusu - GitHub MCP yerel kurulum ===" -ForegroundColor Cyan
Write-Host "Gercek anahtarlar proje klasorune yazilmaz; Windows DPAPI ile kullanici hesabina bagli sifreli saklanir."

$Python = $null
try {
    & py -3.11 --version *> $null
    if ($LASTEXITCODE -eq 0) { $Python = "py" }
} catch {}
if (-not $Python) {
    try {
        & python --version *> $null
        if ($LASTEXITCODE -eq 0) { $Python = "python" }
    } catch {}
}
if (-not $Python) {
    throw "Python bulunamadi. Python 3.11+ kurup tekrar calistirin."
}

$VenvDir = Join-Path $CoreDir ".venv-mcp"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "[1/4] MCP sanal ortami olusturuluyor..."
    if ($Python -eq "py") { & py -3.11 -m venv $VenvDir } else { & python -m venv $VenvDir }
}
Write-Host "[2/4] MCP bagimliliklari kuruluyor/guncelleniyor..."
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $CoreDir "requirements-mcp.txt")

Write-Host "[3/4] GitHub tokeni gerekiyor." -ForegroundColor Yellow
Write-Host "Tarayicida Fine-grained token sayfasi acilacak."
Write-Host "Ayarlar: Resource owner=$ExpectedOwner, Repository access=All repositories, Repository permissions > Administration=Read and write."
Start-Process "https://github.com/settings/personal-access-tokens/new"
$SecureToken = Read-Host "Olusturdugun GitHub tokenini buraya yapistir" -AsSecureString

$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
try {
    $PlainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
    $Headers = @{
        Accept = "application/vnd.github+json"
        Authorization = "Bearer $PlainToken"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent" = "ajan-ordusu-github-mcp-setup"
    }
    $Me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $Headers -Method Get
} finally {
    if ($Bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) }
    $PlainToken = $null
}

if ($Me.login -ne $ExpectedOwner) {
    throw "Token '$($Me.login)' hesabina ait; beklenen hesap '$ExpectedOwner'. Kurulum durduruldu."
}

$TokenPath = Join-Path $SecretDir "github_mcp_token.xml"
$SecureToken | Export-Clixml -Path $TokenPath
@{
    allowed_owner = $ExpectedOwner
    allow_create = $true
    allow_public = $false
    host = "127.0.0.1"
    port = 8765
} | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $ConfigDir "github_mcp.json")

Write-Host "[4/4] GitHub kimligi dogrulandi: $($Me.login)" -ForegroundColor Green
Write-Host "Yerel GitHub MCP hazir. Sonraki adim: setup_openai_tunnel_windows.ps1" -ForegroundColor Green
