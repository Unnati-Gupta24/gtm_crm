<#
.SYNOPSIS
    Work the pipeline: message people who accepted, and watch for replies.

.DESCRIPTION
    Runs only the bottom half of the funnel (check-accepts + send-dms +
    check-replies). Sends no new connection requests, and discovers nobody
    new, so the pipeline only drains.

    Message text comes from OUTREACH_DM_BODY in .env.
    Pace is controlled by OUTREACH_DM_DELAY_SECONDS.

    A thin wrapper around start.ps1 -Mode dm, so the logic lives in exactly
    one place.

.EXAMPLE
    .\dm.ps1
    Dry run. Shows who would be messaged, sends nothing.

.EXAMPLE
    .\dm.ps1 -Live
    Sends messages, looping on OUTREACH_LOOP_MINUTES.

.EXAMPLE
    .\dm.ps1 -Live -Once
    One live pass, then exit.
#>

[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$Once,
    [int]$Every,
    [switch]$SkipSetup
)

& (Join-Path $PSScriptRoot "start.ps1") -Mode dm @PSBoundParameters
exit $LASTEXITCODE
