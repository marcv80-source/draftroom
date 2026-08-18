<#
.SYNOPSIS
  Run a Codex code review with a stable file convention.

.DESCRIPTION
  Standardizes the ad-hoc codex review flow that was previously hand-typed
  per run (and drifted: _raw.txt vs _raw_output.txt, plus a `tail` that fired
  before the output file existed and returned exit 1 even though codex exited 0).

  For a review named <Name> this script always uses, relative to this folder:
    _<Name>_prompt.txt       (input  - must exist)
    _<Name>_raw_output.txt   (output - codex stdout+stderr, overwritten)

  It pipes the prompt into `codex exec --sandbox read-only -`, redirects all
  output to the raw file, prints the real exit code, then tails the raw file
  ONLY if it exists. The tail is a diagnostic and can never be the thing that
  fails the run.

.PARAMETER Name
  Review slug, e.g. 'slice-a'. No leading underscore.

.PARAMETER TailLines
  Lines to show from the end of the raw output. Default 40.

.EXAMPLE
  .\run-codex-review.ps1 -Name slice-a
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Name,

    [int]$TailLines = 40
)

$ErrorActionPreference = 'Stop'

# Resolve paths relative to this script's own folder (the reviews/ dir),
# so the script works regardless of the caller's current directory.
$ReviewsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PromptPath = Join-Path $ReviewsDir ("_{0}_prompt.txt"     -f $Name)
$RawPath    = Join-Path $ReviewsDir ("_{0}_raw_output.txt" -f $Name)

if (-not (Test-Path -LiteralPath $PromptPath)) {
    Write-Error ("Prompt file not found: {0}" -f $PromptPath)
    exit 2
}

Write-Host ("Prompt : {0}" -f $PromptPath)
Write-Host ("Raw out: {0}" -f $RawPath)
Write-Host "Running codex exec --sandbox read-only ..."

# Feed the prompt into codex via stdin ('-') and capture stdout+stderr to the
# raw file. CRITICAL (PS 5.1): do NOT use `codex ... 2>&1 | Out-File` — under
# -ErrorAction Stop, every stderr line codex prints becomes a terminating
# NativeCommandError that aborts the pipeline and leaves an EMPTY file (this is
# exactly what bit the first run). Do the redirection inside cmd.exe at the OS
# level, where stderr is just bytes, and read codex's true exit code from
# %ERRORLEVEL%. `type` streams the prompt file to codex's stdin.
$cmdLine = 'type "{0}" | codex exec --sandbox read-only - > "{1}" 2>&1' -f $PromptPath, $RawPath
& cmd.exe /c $cmdLine
$exit = $LASTEXITCODE
Write-Host ("EXIT={0}" -f $exit)

Write-Host "=== tail of raw output ==="
if (Test-Path -LiteralPath $RawPath) {
    Get-Content -LiteralPath $RawPath -Tail $TailLines
} else {
    Write-Host ("(no raw output file at {0} - codex wrote nothing)" -f $RawPath)
}

# Propagate codex's real exit code so callers/CI see the true result,
# not a tail artifact.
exit $exit
