param([double]$Hours = 1)
$statePath = Join-Path $PSScriptRoot "state.json"
$st = Get-Content $statePath -Raw | ConvertFrom-Json
$now = [DateTime]::UtcNow
$st.soak_hours = $Hours
$st.phase = "soak"
$st.deployed_at = $now.ToString("o")
$st.soak_until = $now.AddHours($Hours).ToString("o")
$st | ConvertTo-Json -Depth 6 | Set-Content $statePath -Encoding utf8
Write-Host "Soak set: ${Hours}h until $($st.soak_until) UTC"