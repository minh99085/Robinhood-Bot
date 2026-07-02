# Sync GitHub main -> VPS and rebuild ONLY the Robinhood Agentic plugin.
# Does NOT touch hermes-trading-engine (Polymarket pulse).
#
# Usage:
#   .\scripts\sync-vps-robinhood.ps1
#   .\scripts\sync-vps-robinhood.ps1 -VerifyOnly

param(
    [switch]$VerifyOnly,
    [string]$SshKey = "$env:USERPROFILE\.ssh\bot2_grok_temp",
    [string]$VpsHost = "45.32.224.147",
    [string]$VpsUser = "root",
    [string]$VpsRepo = "/opt/Grok-Bot-2",
    [string]$PluginPath = "/opt/Grok-Bot-2/hermes-agent-main/plugins/hermes-trading-engine-robinhood"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
Set-Location $RepoRoot

function Get-ShortSha([string]$sha) { if ($sha.Length -ge 7) { $sha.Substring(0, 7) } else { $sha } }

function Invoke-VpsBash([string]$Script) {
    $clean = ($Script -replace "`r`n", "`n" -replace "`r", "").Trim()
    $tmp = Join-Path $env:TEMP ("vps-rh-sync-" + [guid]::NewGuid().ToString("n") + ".sh")
    [System.IO.File]::WriteAllText($tmp, $clean + "`n", [System.Text.UTF8Encoding]::new($false))
    scp -i $SshKey -o StrictHostKeyChecking=no $tmp "${VpsUser}@${VpsHost}:/tmp/vps-remote.sh" | Out-Null
    ssh @sshArgs "bash /tmp/vps-remote.sh; rm -f /tmp/vps-remote.sh"
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
}

& git fetch origin main 2>&1 | Out-Null
$local = (git rev-parse HEAD).Trim()
$origin = (git rev-parse origin/main).Trim()
if ($local -ne $origin) {
    Write-Error "Local HEAD ($local) != origin/main ($origin). Push or pull first."
}

$sshArgs = @("-i", $SshKey, "-o", "ConnectTimeout=20", "-o", "StrictHostKeyChecking=no", "${VpsUser}@${VpsHost}")
$vpsHead = (ssh @sshArgs "git -C $VpsRepo rev-parse HEAD 2>/dev/null || echo MISSING").Trim()

Write-Host "origin/main : $(Get-ShortSha $origin)"
Write-Host "VPS HEAD    : $(Get-ShortSha $vpsHead)"

if ($vpsHead -ne $origin) {
    if ($VerifyOnly) { Write-Error "SYNC FAIL - VPS diverged from origin/main." }
    $bundle = Join-Path $env:TEMP "robinhood-sync.bundle"
    & git bundle create $bundle "HEAD" "^$vpsHead"
    scp -i $SshKey -o StrictHostKeyChecking=no $bundle "${VpsUser}@${VpsHost}:/tmp/robinhood-sync.bundle"
    $remote = @"
set -e
cd $VpsRepo
git fetch /tmp/robinhood-sync.bundle HEAD:refs/remotes/bundle/main
git reset --hard bundle/main
git clean -fd
rm -f /tmp/robinhood-sync.bundle
"@
    Invoke-VpsBash $remote
    Remove-Item -Force $bundle -ErrorAction SilentlyContinue
}

if ($VerifyOnly) {
    if ($vpsHead -eq $origin) { Write-Host "SYNC OK"; exit 0 }
    $vpsAfter = (ssh @sshArgs "git -C $VpsRepo rev-parse HEAD").Trim()
    if ($vpsAfter -eq $origin) { Write-Host "SYNC OK"; exit 0 }
    Write-Error "SYNC FAIL"
}

$docker = @"
set -e
cd $PluginPath
test -f .env || cp .env.example .env
docker compose --profile robinhood down --remove-orphans
docker compose --profile robinhood build
docker compose --profile robinhood up -d --force-recreate --remove-orphans
sleep 6
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'hermes-robinhood'
"@
Invoke-VpsBash $docker
Write-Host "Robinhood plugin deployed (Polymarket engine untouched)."
exit 0