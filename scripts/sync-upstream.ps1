<#
.SYNOPSIS
Resolve upstream sync conflicts locally for DevvGwardo/hermes-agent (maiavm).

.DESCRIPTION
Fetches NousResearch/hermes-agent:main, enables git rerere, creates a
sync/upstream-YYYYMMDD branch from origin/maiavm, and merges upstream/main.
#>
param(
    [string]$UpstreamSha = "",
    [switch]$Push,
    [switch]$OpenPr
)

$ErrorActionPreference = "Stop"

$status = git status --porcelain
if ($status) {
    throw "Working tree is not clean. Commit or stash changes first."
}

git config rerere.enabled true
git config rerere.autoupdate true

if (-not (git remote | Where-Object { $_ -eq "upstream" })) {
    git remote add upstream https://github.com/NousResearch/hermes-agent.git
}

Write-Host "Fetching origin/maiavm and upstream/main..."
git fetch --no-tags origin maiavm
git fetch --no-tags upstream main

$targetSha = if ($UpstreamSha) { $UpstreamSha } else { (git rev-parse refs/remotes/upstream/main).Trim() }
$behind = (git rev-list --count "origin/maiavm..$targetSha").Trim()
Write-Host "Target upstream SHA: $targetSha ($behind commits ahead of origin/maiavm)"

if ([int]$behind -eq 0) {
    Write-Host "maiavm is already up to date."
    exit 0
}

$date = (Get-Date).ToUniversalTime().ToString("yyyyMMdd")
$branch = "sync/upstream-$date"
git checkout -B $branch origin/maiavm

$mergeExit = 0
git merge --no-ff --no-commit $targetSha
if ($LASTEXITCODE -ne 0) { $mergeExit = $LASTEXITCODE }

$conflicts = git diff --name-only --diff-filter=U
if ($conflicts) {
    Write-Warning "Unresolved conflicts in:"
    $conflicts | ForEach-Object { Write-Warning "  - $_" }
    Write-Host ""
    Write-Host "Resolve the conflicts above, run 'git add -A && git commit', then push and open a PR to maiavm."
    exit 1
}

git commit --no-edit -m "sync: merge upstream main into maiavm ($date)"
Write-Host "Merge committed cleanly on branch $branch."

if ($Push -or $OpenPr) {
    git push -u origin $branch
}
if ($OpenPr) {
    gh pr create --repo DevvGwardo/hermes-agent --base maiavm --head $branch `
        --title "sync: merge upstream main into maiavm ($date)" `
        --body "Local upstream sync to ``$targetSha`` ($behind commits)."
}
