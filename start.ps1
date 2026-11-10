<#
.SYNOPSIS
    One script to run the whole LinkedIn outreach campaign.

.DESCRIPTION
    Bootstraps whatever is missing (venv, dependencies, Chromium), verifies the
    LinkedIn session, then runs the campaign on a loop using the timings in
    .env.

    Safe by default: dry-run unless .env sets OUTREACH_DRY_RUN=false or you
    pass -Live.

    This is the engine. search.ps1 and dm.ps1 are thin wrappers around it, so
    the logic lives in exactly one place.

.PARAMETER Mode
    all    - find people, invite, then work the pipeline (default)
    search - discover + invite only. Fills the pipeline.
    dm     - check accepts + send DMs + check replies. Works the pipeline.

.EXAMPLE
    .\start.ps1
    Dry run on a loop. Sends nothing. Start here.

.EXAMPLE
    .\start.ps1 -Live
    Sends for real, looping on OUTREACH_LOOP_MINUTES.

.EXAMPLE
    .\start.ps1 -Mode dm -Live
    Only messages people who already accepted. Sends no new invites.

.EXAMPLE
    .\start.ps1 -Live -Once
    One live pass, then exit. This is what CI uses.
#>

[CmdletBinding()]
param(
    [ValidateSet("all", "search", "dm")]
    [string]$Mode = "all",

    [switch]$Live,      # actually send
    [switch]$Once,      # single pass instead of looping
    [int]$Every,        # minutes between runs; overrides .env
    [switch]$SkipSetup  # skip dependency checks when you know they are fine
)

Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$venvCli    = Join-Path $PSScriptRoot ".venv\Scripts\linkedin-outreach.exe"

function Write-Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "    $message" -ForegroundColor DarkGray }
function Write-Bad($message)  { Write-Host "    $message" -ForegroundColor Yellow }

# Native programs write progress to stderr. Windows PowerShell 5.1 turns that
# into a terminating error when ErrorActionPreference is Stop, even on success,
# so exit codes are the only reliable signal. Run native calls through here.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [string[]]$Arguments = @(),
        [switch]$Quiet
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # Out-Host, not the pipeline: otherwise `$code = Invoke-Native ...`
        # swallows the program's output into the variable.
        if ($Quiet) { & $Exe @Arguments 2>&1 | Out-Null }
        else { & $Exe @Arguments 2>&1 | Out-Host }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Fail($message) {
    Write-Host ""
    Write-Host "  $message" -ForegroundColor Red
    Write-Host ""
    exit 1
}

Write-Host "LinkedIn Outreach CRM" -ForegroundColor White
Write-Host "=====================" -ForegroundColor White

# -----------------------------------------------------------------------------
# 1. Dependencies
# -----------------------------------------------------------------------------
if (-not $SkipSetup) {
    Write-Step "Checking Python environment"

    if (-not (Test-Path $venvPython)) {
        if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
            Fail "Python 3.12 or 3.13 is required but 'python' was not found on PATH."
        }
        Write-Ok "creating .venv"
        if ((Invoke-Native python @("-m", "venv", ".venv")) -ne 0) {
            Fail "Failed to create the virtual environment."
        }
    }

    # Importability is a better readiness check than looking for files.
    if ((Invoke-Native $venvPython @("-c", "import outreach, openpyxl, mcp, playwright") -Quiet) -ne 0) {
        Write-Ok "installing dependencies (a few minutes the first time)"
        Invoke-Native $venvPython @("-m", "pip", "install", "--upgrade", "pip", "--quiet") -Quiet | Out-Null
        if ((Invoke-Native $venvPython @("-m", "pip", "install", "-e", ".", "--quiet")) -ne 0) {
            Fail "Dependency installation failed."
        }
    }
    Write-Ok "python environment ready"

    $chromiumCheck = "import os,sys;from pathlib import Path;p=Path(os.environ.get('LOCALAPPDATA',''))/'ms-playwright';sys.exit(0 if p.exists() and any(p.glob('chromium*')) else 1)"
    if ((Invoke-Native $venvPython @("-c", $chromiumCheck) -Quiet) -ne 0) {
        Write-Ok "installing Chromium"
        if ((Invoke-Native $venvPython @("-m", "playwright", "install", "chromium")) -ne 0) {
            Fail "Chromium installation failed."
        }
    }
    Write-Ok "chromium ready"
}

# -----------------------------------------------------------------------------
# 2. Configuration
# -----------------------------------------------------------------------------
Write-Step "Checking configuration"

if (-not (Test-Path ".env")) {
    if (-not (Test-Path ".env.example")) { Fail ".env.example is missing; cannot create .env." }
    Copy-Item ".env.example" ".env"
    Write-Bad "created .env from .env.example"
    Write-Host ""
    Write-Host "  Edit .env before going live - your search terms and message text live there." -ForegroundColor Yellow
    Write-Host "  Then run this script again." -ForegroundColor Yellow
    Write-Host ""
    exit 0
}
Write-Ok ".env present"

# -----------------------------------------------------------------------------
# 3. LinkedIn session
# -----------------------------------------------------------------------------
Write-Step "Checking LinkedIn session"

$doctor = & {
    $ErrorActionPreference = "Continue"
    & $venvCli doctor 2>&1
}
$doctorCode = $LASTEXITCODE

if ($doctorCode -ne 0) {
    # Only surface the detail when something is actually wrong.
    $doctor | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
    Write-Host ""
    Write-Bad "LinkedIn session is not ready."
    Write-Host "    Sign in once with:  .\.venv\Scripts\linkedin-mcp.exe login" -ForegroundColor Yellow
    Write-Host "    Then run this script again." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}
Write-Ok "session ready"

# -----------------------------------------------------------------------------
# 4. Run
# -----------------------------------------------------------------------------
$runArgs = @()
if ($Once) { $runArgs += "run-once" } else { $runArgs += "run-loop" }
$runArgs += @("--stage", $Mode)
if ($Live) { $runArgs += "--live" }
if ($Every -and -not $Once) { $runArgs += @("--every", "$Every") }

$what = switch ($Mode) {
    "search" { "finding people and sending connection requests" }
    "dm"     { "messaging people who accepted, and checking replies" }
    default  { "the full pipeline" }
}
$suffix = if ($Live) { " (LIVE - sending for real)" } else { " (dry run - sending nothing)" }
Write-Step ("Starting " + $what + $suffix)
if (-not $Live) { Write-Ok "no invites or messages will be sent; pass -Live when ready" }
Write-Host ""

# Called directly rather than through Invoke-Native so the campaign output
# streams to the console (and stays pipeable) instead of being captured.
$ErrorActionPreference = "Continue"
& $venvCli @runArgs
exit $LASTEXITCODE
