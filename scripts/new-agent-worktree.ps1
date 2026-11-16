# Create an isolated sibling worktree for one coding agent.
# Usage: .\scripts\new-agent-worktree.ps1 -Slug matching
#        .\scripts\new-agent-worktree.ps1 -Slug profile-search -StartPoint origin/master
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9]+(-[a-z0-9]+)*$')]
    [string]$Slug,

    [string]$StartPoint = "HEAD",

    [ValidateSet("feat", "fix")]
    [string]$Kind = "feat"
)

$ErrorActionPreference = "Stop"

# Live DB is job_app_agent_<slug>; pytest uses job_app_agent_test[_<slug>].
if ($Slug -eq "test" -or $Slug.StartsWith("test-")) {
    throw "slug '$Slug' collides with pytest DB names; choose another"
}

$primary = (git rev-parse --show-toplevel).Trim()
if (-not $primary) {
    throw "not inside a git repository"
}

$parent = Split-Path -Parent $primary
$path = Join-Path $parent "jaa-$Slug"
$branch = "$Kind/$Slug"

if (Test-Path $path) {
    throw "worktree path already exists: $path"
}

git worktree add -b $branch $path $StartPoint
if ($LASTEXITCODE -ne 0) {
    throw "git worktree add failed"
}

$srcEnv = Join-Path $primary ".env"
$dstEnv = Join-Path $path ".env"
if (Test-Path $srcEnv) {
    Push-Location $primary
    try {
        uv run python scripts/write_worktree_env.py --src $srcEnv --dst $dstEnv --slug $Slug
        if ($LASTEXITCODE -ne 0) {
            throw "write_worktree_env.py failed"
        }
    } catch {
        git worktree remove --force $path 2>$null
        git branch -D $branch 2>$null
        throw
    } finally {
        Pop-Location
    }
} else {
    Write-Host "No .env in primary; create one in $path with a slug-named DATABASE_URL before running the app or alembic."
}

Write-Host "Worktree ready: $path"
Write-Host "Branch: $branch"
Write-Host "Pytest DB name: from tests/isolation.py for directory jaa-$Slug"
Write-Host "Open this folder in its own Cursor chat/agent. Do not edit $primary for this task."
