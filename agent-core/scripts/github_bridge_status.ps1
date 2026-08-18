function Test-Port([int]$Port) {
    try { $c = New-Object System.Net.Sockets.TcpClient; $c.Connect("127.0.0.1",$Port); $ok=$c.Connected; $c.Close(); return $ok } catch { return $false }
}
$Mcp = Test-Port 8765
$Tunnel = $false
try { $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8877/readyz" -TimeoutSec 2; $Tunnel = ($r.StatusCode -eq 200) } catch {}
Write-Host ("GitHub MCP : " + $(if($Mcp){"OK"}else{"KAPALI"}))
Write-Host ("OpenAI Tunnel: " + $(if($Tunnel){"READY"}else{"HAZIR DEGIL/KAPALI"}))
