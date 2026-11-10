<#
.SYNOPSIS
    Fill the pipeline: find matching people and send connection requests.

.DESCRIPTION
    Runs only the top half of the funnel (discover + invite). Sends no
    messages. Use this when you want to add prospects without touching
    anyone already in the pipeline.

    Search terms come from OUTREACH_SEARCH_* in .env.
    Pace is controlled by OUTREACH_INVITE_DELAY_SECONDS.

    A thin wrapper around start.ps1 -Mode search, so the logic lives in
    exactly one place.

.EXAMPLE
    .\search.ps1
    Dry run. Shows who would be invited, sends nothing.

.EXAMPLE
    .\search.ps1 -Live
    Sends connection requests, looping on OUTREACH_LOOP_MINUTES.

.EXAMPLE
    .\search.ps1 -Live -Once
    One live pass, then exit.
#>

[CmdletBinding()]
param(
    [switch]$Live,
    [switch]$Once,
    [int]$Every,
    [switch]$SkipSetup
)

& (Join-Path $PSScriptRoot "start.ps1") -Mode search @PSBoundParameters
exit $LASTEXITCODE
