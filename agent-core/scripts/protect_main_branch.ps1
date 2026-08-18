param(
    [string]$Owner = "Piardian",
    [string]$Repo = "orkestrat-r",
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$TokenPath = Join-Path $env:LOCALAPPDATA "AjanOrdusu\secrets\github_mcp_token.xml"
if (-not (Test-Path $TokenPath)) {
    throw "GitHub MCP token bulunamadi: $TokenPath. Once SETUP_GITHUB_REPO_BRIDGE.bat calistirin."
}

$SecureToken = Import-Clixml -Path $TokenPath
$Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
$PlainToken = $null
try {
    $PlainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr)
    $Headers = @{
        Accept = "application/vnd.github+json"
        Authorization = "Bearer $PlainToken"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent" = "ajan-ordusu-branch-protection"
    }

    $Me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $Headers -Method Get
    if ($Me.login -ne $Owner) {
        throw "Token '$($Me.login)' hesabina ait; beklenen hesap '$Owner'."
    }

    $Checks = @(
        "full-regression",
        "postgres-safety",
        "dependency-lock",
        "crewai-contract",
        "openhands-contract",
        "reliability-smoke (ubuntu-latest, 3.11)",
        "reliability-smoke (ubuntu-latest, 3.12)",
        "reliability-smoke (windows-latest, 3.11)",
        "reliability-smoke (windows-latest, 3.12)"
    )

    $Body = @{
        required_status_checks = @{
            strict = $true
            contexts = $Checks
        }
        enforce_admins = $true
        required_pull_request_reviews = @{
            dismiss_stale_reviews = $true
            require_code_owner_reviews = $false
            required_approving_review_count = 0
            require_last_push_approval = $false
        }
        restrictions = $null
        required_linear_history = $false
        allow_force_pushes = $false
        allow_deletions = $false
        block_creations = $false
        required_conversation_resolution = $true
        lock_branch = $false
        allow_fork_syncing = $false
    } | ConvertTo-Json -Depth 8

    $OwnerEscaped = [Uri]::EscapeDataString($Owner)
    $RepoEscaped = [Uri]::EscapeDataString($Repo)
    $BranchEscaped = [Uri]::EscapeDataString($Branch)
    $Uri = "https://api.github.com/repos/$OwnerEscaped/$RepoEscaped/branches/$BranchEscaped/protection"
    $Result = Invoke-RestMethod -Uri $Uri -Headers $Headers -Method Put -ContentType "application/json" -Body $Body

    Write-Host ""
    Write-Host "MAIN BRANCH KORUMASI AKTIF" -ForegroundColor Green
    Write-Host "Repo: $Owner/$Repo"
    Write-Host "Branch: $Branch"
    Write-Host "PR zorunlu: EVET"
    Write-Host "Admin kurallara tabi: EVET"
    Write-Host "Force push: KAPALI"
    Write-Host "Branch silme: KAPALI"
    Write-Host "Required checks:"
    foreach ($Check in $Checks) { Write-Host "  - $Check" }
} catch {
    Write-Host ""
    Write-Host "Branch protection uygulanamadi." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($Bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) }
    $PlainToken = $null
}
