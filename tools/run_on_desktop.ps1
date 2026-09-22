<#
Run a command on the INTERACTIVE console desktop, from a non-interactive SSH session.

Why this exists, and why skipping it would waste a day:

An SSH login on Windows gets its own session and window station. Two things we need are
scoped to a desktop, and both fail SILENTLY across that boundary:

  * SendNotifyMessage(HWND_BROADCAST, ...) reaches top-level windows on the CALLING
    thread's desktop only. From SSH, iRacing's window is not among them.
  * SendInput delivers to the foreground window of ITS window station, which from SSH
    is not the one with the sim on it.

Run the replay bench straight over SSH and every message reports success while reaching
nothing, which reads exactly like "the sim ignores replay commands": the same class of
false verdict a stale agent exe produced on 2026-07-25.

This schedules the command as the logged-on user, interactively, so it executes where
the sim actually is, then hands back its output.

    .\tools\run_on_desktop.ps1 -Command "C:\Python\python.exe tools\replay_bench.py --send"
    .\tools\run_on_desktop.ps1 -Command "..." -TimeoutSec 180
#>
param(
    # Prefer -ScriptFile. Passing a command as a STRING has to survive bash -> ssh ->
    # PowerShell -> generated-script quoting, and it does not: "$PID" arrived as the
    # literal "\25032" and the run died. A file on disk has no quoting chain at all.
    [string]$ScriptFile,
    [string]$Command,
    [string]$WorkingDir = $PWD.Path,
    [int]$TimeoutSec = 120,
    [string]$TaskName = "pylon_desktop_run"
)

if (-not $ScriptFile -and -not $Command) {
    throw "give either -ScriptFile <path.ps1> or -Command <string>"
}
if ($ScriptFile) {
    $ScriptFile = (Resolve-Path $ScriptFile).Path
}

$ErrorActionPreference = "Stop"

$out = Join-Path $env:TEMP "pylon_desktop_run.out"
$script = Join-Path $env:TEMP "pylon_desktop_run.ps1"
Remove-Item $out, $script -ErrorAction SilentlyContinue

# Generate a script file rather than an -EncodedCommand one-liner.
#
# The one-liner form was "& $Command *>&1 | Tee-Object -FilePath $out", and the
# redirection binds to only the LAST statement of $Command, so a multi-statement
# command silently lost most of its output, and a failing one produced no file at all,
# which is indistinguishable from "the task never ran".
#
# Start-Transcript captures everything including native stdout/stderr, so the log
# exists even when the command dies early.
$body = if ($ScriptFile) { "& '$ScriptFile'" } else { $Command }

@"
Set-Location '$WorkingDir'
`$ErrorActionPreference = 'Continue'
Start-Transcript -Path '$out' -Force | Out-Null
try {
    $body
} catch {
    Write-Output "EXCEPTION: `$_"
}
Stop-Transcript | Out-Null
"@ | Set-Content -Path $script -Encoding UTF8

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$script`""

# INTERACTIVE is the whole point: it binds the task to the logged-on user's session,
# which is the one holding the sim.
#
# Identify the user by SID, not by name. On a workgroup machine $env:USERDOMAIN is
# "WORKGROUP", and "WORKGROUP\user" resolves to nothing: Register-ScheduledTask fails
# with "No mapping between account names and security IDs was done". The SID is exact
# and needs no domain at all.
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$principal = New-ScheduledTaskPrincipal -UserId $sid `
    -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal `
    -Force | Out-Null

try {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Milliseconds 700

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        $state = (Get-ScheduledTask -TaskName $TaskName).State
        if ($state -ne "Running") { break }
        Start-Sleep -Milliseconds 500
    }

    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
        Write-Warning "still running after ${TimeoutSec}s; output so far:"
    }

    if (Test-Path $out) {
        Get-Content $out
    } else {
        # No output file means the task never actually started in a session. Almost
        # always: nobody is logged on at the console, so there is no interactive
        # session to run in, and therefore no desktop with the sim on it either.
        Write-Warning "no output produced. Is a user logged on at the console?"
        Write-Warning "LastTaskResult=$($info.LastTaskResult) LastRunTime=$($info.LastRunTime)"
    }
} finally {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
}
