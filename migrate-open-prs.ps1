```powershell
# ============================================================
# Moonraker Timelapse - Open PR Migration
#
# Original:
#   mainsail-crew/moonraker-timelapse
#
# Remastered:
#   MoltenOre/moonraker-timelapse-remastered
#
# This script:
#   - Finds all OPEN PRs targeting main
#   - Fetches each PR's actual Git reference
#   - Creates a local imported branch
#   - Pushes that branch to the remastered repository
#   - Creates a new PR against remastered/main
#
# The original repository is NEVER modified.
# ============================================================

$ErrorActionPreference = "Stop"

# Git writes some normal diagnostic messages to stderr.
# Prevent PowerShell from treating those as terminating errors.
$PSNativeCommandUseErrorActionPreference = $false

# ============================================================
# CONFIGURATION
# ============================================================

$OriginalOwner = "mainsail-crew"
$OriginalRepo  = "moonraker-timelapse"

$RemasterOwner = "MoltenOre"
$RemasterRepo  = "moonraker-timelapse-remastered"

$OriginalRemote = "upstream"
$RemasterRemote = "origin"

$BaseBranch = "main"
$BranchPrefix = "imported/pr"

$ApiVersion = "2022-11-28"

# ============================================================
# AUTHENTICATION
# ============================================================

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Moonraker Timelapse - PR Migration" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "A GitHub Personal Access Token is required."
Write-Host ""
Write-Host "The token needs access to:"
Write-Host "  - Read the original repository"
Write-Host "  - Write to the remastered repository"
Write-Host "  - Create pull requests"
Write-Host ""

$Token = Read-Host "GitHub Token"

$Headers = @{
    "Accept"               = "application/vnd.github+json"
    "Authorization"        = "Bearer $Token"
    "X-GitHub-Api-Version" = $ApiVersion
}

# ============================================================
# API HELPER
# ============================================================

function Invoke-GitHubApi {
    param (
        [string]$Method,
        [string]$Url,
        $Body = $null
    )

    if ($null -ne $Body) {

        $Json = $Body | ConvertTo-Json -Depth 20

        return Invoke-RestMethod `
            -Method $Method `
            -Uri $Url `
            -Headers $Headers `
            -ContentType "application/json" `
            -Body $Json
    }

    return Invoke-RestMethod `
        -Method $Method `
        -Uri $Url `
        -Headers $Headers
}

# ============================================================
# BRANCH NAME SANITIZER
# ============================================================

function Convert-ToSafeBranchName {
    param (
        [string]$Name
    )

    $Name = $Name.ToLower()

    # Replace whitespace
    $Name = $Name -replace '\s+', '-'

    # Remove characters that are problematic in Git refs
    $Name = $Name -replace '[^a-z0-9._/-]', '-'

    # Collapse repeated hyphens
    $Name = $Name -replace '-+', '-'

    # Avoid repeated slashes
    $Name = $Name -replace '/+', '/'

    # Remove leading/trailing punctuation
    $Name = $Name.Trim('-', '/')

    return $Name
}

# ============================================================
# GET ALL OPEN PRs
# ============================================================

function Get-AllOpenPRs {

    $AllPRs = @()
    $Page = 1

    while ($true) {

        $Url = "https://api.github.com/repos/$OriginalOwner/$OriginalRepo/pulls?state=open&base=$BaseBranch&per_page=100&page=$Page"

        Write-Host "Fetching PR page $Page..." -ForegroundColor DarkGray

        $PRs = Invoke-GitHubApi `
            -Method "GET" `
            -Url $Url

        if ($null -eq $PRs -or $PRs.Count -eq 0) {
            break
        }

        $AllPRs += $PRs

        if ($PRs.Count -lt 100) {
            break
        }

        $Page++
    }

    return $AllPRs
}

# ============================================================
# CHECK REMASTER PR
# ============================================================

function Get-ExistingRemasterPR {
    param (
        [string]$BranchName
    )

    $EncodedHead = [Uri]::EscapeDataString(
        "$RemasterOwner`:$BranchName"
    )

    $Url = "https://api.github.com/repos/$RemasterOwner/$RemasterRepo/pulls?state=all&base=$BaseBranch&head=$EncodedHead&per_page=100"

    try {
        $Results = Invoke-GitHubApi `
            -Method "GET" `
            -Url $Url

        if ($Results.Count -gt 0) {
            return $Results[0]
        }
    }
    catch {
        # No existing PR is fine
    }

    return $null
}

# ============================================================
# CHECK REMASTER BRANCH
# ============================================================

function Test-RemoteBranchExists {
    param (
        [string]$BranchName
    )

    $UrlEncoded = [Uri]::EscapeDataString($BranchName)

    $Url = "https://api.github.com/repos/$RemasterOwner/$RemasterRepo/git/ref/heads/$UrlEncoded"

    try {

        Invoke-GitHubApi `
            -Method "GET" `
            -Url $Url | Out-Null

        return $true
    }
    catch {
        return $false
    }
}

# ============================================================
# CREATE REMASTER PR
# ============================================================

function New-RemasterPR {
    param (
        $OriginalPR,
        [string]$NewBranch
    )

    $OriginalUrl = $OriginalPR.html_url

    $OriginalBody = $OriginalPR.body

    if ([string]::IsNullOrWhiteSpace($OriginalBody)) {
        $OriginalBody = "_No description was provided in the original PR._"
    }

    $NewBody = @"
## Imported from moonraker-timelapse

This pull request was migrated from the original
moonraker-timelapse repository.

### Original PR

**PR:** #$($OriginalPR.number)

**Original:** $OriginalUrl

**Author:** @$($OriginalPR.user.login)

---

### Original description

$OriginalBody

---

> This PR was automatically migrated into the remastered project.
"@

    $Url = "https://api.github.com/repos/$RemasterOwner/$RemasterRepo/pulls"

    $Body = @{
        title = $OriginalPR.title
        head  = $NewBranch
        base  = $BaseBranch
        body  = $NewBody
        draft = $OriginalPR.draft
    }

    Write-Host ""
    Write-Host "Creating remastered PR..." -ForegroundColor Green

    $NewPR = Invoke-GitHubApi `
        -Method "POST" `
        -Url $Url `
        -Body $Body

    # --------------------------------------------------------
    # Copy labels
    # --------------------------------------------------------

    if ($OriginalPR.labels.Count -gt 0) {

        $LabelNames = @()

        foreach ($Label in $OriginalPR.labels) {
            $LabelNames += $Label.name
        }

        $LabelUrl = "https://api.github.com/repos/$RemasterOwner/$RemasterRepo/issues/$($NewPR.number)/labels"

        $LabelBody = @{
            labels = $LabelNames
        }

        try {

            Invoke-GitHubApi `
                -Method "POST" `
                -Url $LabelUrl `
                -Body $LabelBody | Out-Null

            Write-Host "Labels copied." -ForegroundColor DarkGray
        }
        catch {

            Write-Host "Warning: Could not copy labels." -ForegroundColor Yellow
        }
    }

    return $NewPR
}

# ============================================================
# VERIFY GIT REMOTES
# ============================================================

Write-Host ""
Write-Host "Checking Git remotes..." -ForegroundColor Cyan

$Remotes = git remote

if ($Remotes -notcontains $OriginalRemote) {

    Write-Host ""
    Write-Host "ERROR: '$OriginalRemote' remote does not exist." -ForegroundColor Red
    Write-Host ""
    Write-Host "Run:"
    Write-Host ""
    Write-Host "git remote add upstream https://github.com/mainsail-crew/moonraker-timelapse.git"
    Write-Host ""
    exit 1
}

if ($Remotes -notcontains $RemasterRemote) {

    Write-Host ""
    Write-Host "ERROR: '$RemasterRemote' remote does not exist." -ForegroundColor Red
    exit 1
}

# ============================================================
# VERIFY CURRENT BRANCH
# ============================================================

$CurrentBranch = git branch --show-current

if ($CurrentBranch -ne $BaseBranch) {

    Write-Host ""
    Write-Host "You are currently on '$CurrentBranch'." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "Switching to main..."

    git switch $BaseBranch
}

# ============================================================
# FETCH ORIGINAL
# ============================================================

Write-Host ""
Write-Host "Fetching original repository..." -ForegroundColor Cyan

git fetch $OriginalRemote

if ($LASTEXITCODE -ne 0) {
    throw "Failed to fetch original repository."
}

# ============================================================
# UPDATE REMASTER MAIN
# ============================================================

Write-Host ""
Write-Host "Updating remastered main..." -ForegroundColor Cyan

git fetch $RemasterRemote

git pull $RemasterRemote $BaseBranch

if ($LASTEXITCODE -ne 0) {
    throw "Failed to update remastered main."
}

# ============================================================
# GET OPEN PRs
# ============================================================

Write-Host ""
Write-Host "Getting open PRs targeting main..." -ForegroundColor Cyan

$PRs = Get-AllOpenPRs

Write-Host ""
Write-Host "Found $($PRs.Count) open PR(s)." -ForegroundColor Green

if ($PRs.Count -eq 0) {

    Write-Host ""
    Write-Host "Nothing to migrate." -ForegroundColor Green
    exit
}

# ============================================================
# DISPLAY PRs
# ============================================================

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " PRs to migrate" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

foreach ($PR in $PRs) {

    $DraftText = ""

    if ($PR.draft) {
        $DraftText = " [DRAFT]"
    }

    Write-Host "#$($PR.number)$DraftText - $($PR.title)"
    Write-Host "    $($PR.html_url)" -ForegroundColor DarkGray
}

Write-Host ""

$Confirm = Read-Host "Type YES to begin migration"

if ($Confirm -ne "YES") {

    Write-Host ""
    Write-Host "Migration cancelled."
    exit
}

# ============================================================
# MIGRATION
# ============================================================

$Results = @()

foreach ($PR in $PRs) {

    Write-Host ""
    Write-Host ""
    Write-Host "==================================================" -ForegroundColor Cyan
    Write-Host " PR #$($PR.number)" -ForegroundColor Cyan
    Write-Host " $($PR.title)" -ForegroundColor Cyan
    Write-Host "==================================================" -ForegroundColor Cyan

    # --------------------------------------------------------
    # Build new branch name
    # --------------------------------------------------------

    $OriginalBranch = $PR.head.ref

    $SafeBranch = Convert-ToSafeBranchName $OriginalBranch

    $NewBranch = "$BranchPrefix/$($PR.number)-$SafeBranch"

    Write-Host ""
    Write-Host "Original branch:"
    Write-Host "  $OriginalBranch"

    Write-Host ""
    Write-Host "New branch:"
    Write-Host "  $NewBranch"

    # --------------------------------------------------------
    # Check if already migrated
    # --------------------------------------------------------

    $ExistingPR = Get-ExistingRemasterPR $NewBranch

    if ($null -ne $ExistingPR) {

        Write-Host ""
        Write-Host "Already migrated." -ForegroundColor Yellow
        Write-Host "Existing PR: $($ExistingPR.html_url)"

        $Results += [PSCustomObject]@{
            OriginalPR = $PR.number
            NewPR      = $ExistingPR.number
            Branch     = $NewBranch
            Status     = "Already exists"
        }

        continue
    }

    # --------------------------------------------------------
    # Check whether remote branch already exists
    # --------------------------------------------------------

    if (Test-RemoteBranchExists $NewBranch) {

        Write-Host ""
        Write-Host "Branch already exists on remastered repository." -ForegroundColor Yellow
        Write-Host "Skipping branch creation."

    }
    else {

        # ----------------------------------------------------
        # FETCH PR HEAD
        #
        # GitHub exposes PR heads as:
        #
        # refs/pull/<PR NUMBER>/head
        #
        # This gives us the actual PR commit history.
        # ----------------------------------------------------

        Write-Host ""
        Write-Host "Fetching PR commits..." -ForegroundColor Cyan

        $TempBranch = "migration-pr-$($PR.number)"

        # Remove old local migration branch if it exists.
        # Do not fail if it doesn't exist.
        $ExistingTempBranch = git branch --list $TempBranch

        if ($ExistingTempBranch) {
            git branch -D $TempBranch
        }

        git fetch `
            $OriginalRemote `
            "pull/$($PR.number)/head:$TempBranch"

        if ($LASTEXITCODE -ne 0) {

            Write-Host ""
            Write-Host "FAILED to fetch PR #$($PR.number)." -ForegroundColor Red

            $Results += [PSCustomObject]@{
                OriginalPR = $PR.number
                NewPR      = ""
                Branch     = $NewBranch
                Status     = "FAILED - fetch"
            }

            continue
        }

        # ----------------------------------------------------
        # CREATE NEW BRANCH
        # ----------------------------------------------------

        Write-Host ""
        Write-Host "Creating remastered branch..." -ForegroundColor Cyan

        git branch $NewBranch $TempBranch

        if ($LASTEXITCODE -ne 0) {

            Write-Host ""
            Write-Host "FAILED to create branch." -ForegroundColor Red

            $Results += [PSCustomObject]@{
                OriginalPR = $PR.number
                NewPR      = ""
                Branch     = $NewBranch
                Status     = "FAILED - branch"
            }

            git branch -D $TempBranch 2>$null

            continue
        }

        # ----------------------------------------------------
        # PUSH BRANCH TO REMASTERED
        # ----------------------------------------------------

        Write-Host ""
        Write-Host "Pushing branch to remastered repository..." -ForegroundColor Cyan

        git push $RemasterRemote "$NewBranch`:$NewBranch"

        if ($LASTEXITCODE -ne 0) {

            Write-Host ""
            Write-Host "FAILED to push branch." -ForegroundColor Red

            $Results += [PSCustomObject]@{
                OriginalPR = $PR.number
                NewPR      = ""
                Branch     = $NewBranch
                Status     = "FAILED - push"
            }

            git branch -D $NewBranch 2>$null
            git branch -D $TempBranch 2>$null

            continue
        }

        Write-Host ""
        Write-Host "Branch pushed successfully." -ForegroundColor Green

        # ----------------------------------------------------
        # CLEAN TEMP BRANCH
        # ----------------------------------------------------

        git branch -D $TempBranch 2>$null
    }

    # --------------------------------------------------------
    # CREATE NEW PR
    # --------------------------------------------------------

    try {

        $NewPR = New-RemasterPR `
            -OriginalPR $PR `
            -NewBranch $NewBranch

        Write-Host ""
        Write-Host "==================================================" -ForegroundColor Green
        Write-Host " MIGRATED SUCCESSFULLY" -ForegroundColor Green
        Write-Host "==================================================" -ForegroundColor Green

        Write-Host ""
        Write-Host "Original PR:"
        Write-Host "  $($PR.html_url)"

        Write-Host ""
        Write-Host "New PR:"
        Write-Host "  $($NewPR.html_url)" -ForegroundColor Green

        $Results += [PSCustomObject]@{
            OriginalPR = $PR.number
            NewPR      = $NewPR.number
            Branch     = $NewBranch
            Status     = "Migrated"
        }

    }
    catch {

        Write-Host ""
        Write-Host "FAILED to create new PR." -ForegroundColor Red
        Write-Host $_.Exception.Message -ForegroundColor Red

        $Results += [PSCustomObject]@{
            OriginalPR = $PR.number
            NewPR      = ""
            Branch     = $NewBranch
            Status     = "FAILED - PR creation"
        }
    }
}

# ============================================================
# RESULTS
# ============================================================

Write-Host ""
Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " MIGRATION COMPLETE" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

$Results | Format-Table -AutoSize

Write-Host ""
Write-Host "Original repository was NOT modified." -ForegroundColor Green
Write-Host ""
Write-Host "All successfully migrated PRs now exist as branches"
Write-Host "under the remastered repository."
Write-Host ""
```
