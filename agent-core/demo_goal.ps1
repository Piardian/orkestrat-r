param(
    [Parameter(Mandatory=$true)]
    [string]$Workspace,

    [Parameter(Mandatory=$true)]
    [string]$Task,

    [switch]$Json
)

$ErrorActionPreference = "Stop"
$core = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $core ".venv-openhands\Scripts\python.exe"
$runner = Join-Path $core "run_demo_goal_dns.py"

if (-not (Test-Path $python)) {
    throw "OpenHands virtualenv not found: $python"
}

if (-not (Test-Path $runner)) {
    throw "Demo runner not found: $runner"
}

$argsList = @($runner, "--workspace", $Workspace, "--task", $Task)
if ($Json) {
    $argsList += "--json"
}

& $python @argsList
exit $LASTEXITCODE
