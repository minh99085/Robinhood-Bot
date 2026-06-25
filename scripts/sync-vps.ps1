# Sync GitHub main -> VPS (git bundle). Keeps SHA-for-SHA: origin/main == VPS HEAD.
# Every deploy rebuilds containers (down --remove-orphans -> build -> up -d --remove-orphans).
# Usage:
#   .\scripts\sync-vps.ps1              # sync + rebuild (default)
#   .\scripts\sync-vps.ps1 -SkipRebuild # sync code only (rare)
#   .\scripts\sync-vps.ps1 -VerifyOnly  # check SHAs, exit 1 if diverged
#   .\scripts\sync-vps.ps1 -Rebuild     # deprecated alias; rebuild is already default

param(
    [switch]$SkipRebuild,
    [switch]$Rebuild,
    [switch]$VerifyOnly,
    [string]$SshKey = "$env:USERPROFILE\.ssh\bot2_grok_temp",
    [string]$VpsHost = "45.32.224.147",
    [string]$VpsUser = "root",
    [string]$VpsRepo = "/opt/Grok-Bot-2",
    [string]$PluginPath = "/opt/Grok-Bot-2/hermes-agent-main/plugins/hermes-trading-engine"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path $PSScriptRoot -Parent
if (-not (Test-Path (Join-Path $RepoRoot ".git"))) {
    Write-Error "Not a git repo: $RepoRoot"
}
Set-Location $RepoRoot

function Get-ShortSha([string]$sha) { if ($sha.Length -ge 7) { $sha.Substring(0, 7) } else { $sha } }

$doRebuild = -not $SkipRebuild

Write-Host "Repo: $RepoRoot"
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& git fetch origin main 2>&1 | Out-Null
$ErrorActionPreference = $prevEap
$local = (git rev-parse HEAD).Trim()
$origin = (git rev-parse origin/main).Trim()

if ($local -ne $origin) {
    Write-Error "Local HEAD ($local) != origin/main ($origin). Push or pull first."
}

$sshArgs = @("-i", $SshKey, "-o", "ConnectTimeout=20", "-o", "StrictHostKeyChecking=no", "${VpsUser}@${VpsHost}")
$vpsHead = (ssh @sshArgs "git -C $VpsRepo rev-parse HEAD 2>/dev/null || echo MISSING").Trim()

Write-Host "origin/main : $(Get-ShortSha $origin) $origin"
Write-Host "VPS HEAD    : $(Get-ShortSha $vpsHead) $vpsHead"

if ($vpsHead -eq $origin) {
    Write-Host "SYNC OK - VPS already matches origin/main."
    if ($VerifyOnly) { exit 0 }
    if (-not $doRebuild) {
        Write-Host "SkipRebuild set; not recreating containers."
        exit 0
    }
    Write-Host "Rebuilding containers (down --remove-orphans -> build -> up -d --remove-orphans)..."
} elseif ($VerifyOnly) {
    Write-Error "SYNC FAIL - VPS diverged from origin/main."
}

if ($vpsHead -eq "MISSING" -or $vpsHead.Length -lt 40) {
    Write-Error "VPS repo not found at $VpsRepo. Clone via bundle first."
}

$bundle = Join-Path $env:TEMP "grok-bot2-sync.bundle"
if ($vpsHead -ne $origin) {
    Write-Host "Creating bundle $vpsHead..$origin ..."
    & git bundle create $bundle "HEAD" "^$vpsHead"
    if (-not (Test-Path $bundle)) {
        Write-Error "Bundle creation failed (empty range?). VPS=$vpsHead origin=$origin"
    }
    scp -i $SshKey -o StrictHostKeyChecking=no $bundle "${VpsUser}@${VpsHost}:/tmp/grok-bot2-sync.bundle"
    $remote = @"
set -e
cd $VpsRepo
git fetch /tmp/grok-bot2-sync.bundle HEAD:refs/remotes/bundle/main
git reset --hard bundle/main
git clean -fd
rm -f /tmp/grok-bot2-sync.bundle
echo VPS_HEAD=`$(git rev-parse HEAD)
"@
    ssh @sshArgs $remote
    Remove-Item -Force $bundle -ErrorAction SilentlyContinue
}

if ($doRebuild) {
    $docker = @"
set -e
cd $PluginPath
docker compose down --remove-orphans
docker compose build
docker compose up -d --remove-orphans
sleep 8
docker ps --format '{{.Names}} {{.Status}}' | grep -E 'hermes-training|hermes-trading-engine'
"@
    ssh @sshArgs $docker
}

$vpsAfter = (ssh @sshArgs "git -C $VpsRepo rev-parse HEAD").Trim()
if ($vpsAfter -ne $origin) {
    Write-Error "SYNC FAIL after deploy: VPS=$vpsAfter origin=$origin"
}

Write-Host "SYNC OK - VPS HEAD matches origin/main ($(Get-ShortSha $origin))."
if ($doRebuild) {
    Write-Host "Containers rebuilt with --remove-orphans."
}
exit 0