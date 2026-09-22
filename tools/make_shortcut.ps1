<#
Put Pylon on the Desktop, so starting a broadcast is a double-click rather than a
remembered command in a terminal.

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\make_shortcut.ps1

This is for people running Pylon from a SOURCE CHECKOUT. An installed copy gets
its shortcuts from the installer (packaging\pylon.iss) and does not need this.

Two shortcuts, both pointing into this checkout:

    Pylon                  starts the show (tools\studio.bat)
    Pylon - Check my setup runs the doctor and holds the window open

Both set WorkingDirectory to the checkout, which is what lets the .bat find the
environment: without it a double-clicked shortcut runs from C:\Windows\System32
and nothing resolves.
#>
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [string]$Desktop = [Environment]::GetFolderPath("Desktop"),
    [switch]$Remove
)

$ErrorActionPreference = "Stop"

$links = @(
    @{ Name = "Pylon";                  Target = "$Root\tools\studio.bat";
       Desc = "Start the Pylon broadcast director" },
    @{ Name = "Pylon - Check my setup"; Target = "$Root\tools\doctor.bat";
       Desc = "Check OBS, iRacing, the scenes and the ports" }
)

if ($Remove) {
    foreach ($l in $links) {
        $p = Join-Path $Desktop "$($l.Name).lnk"
        if (Test-Path $p) { Remove-Item $p; Write-Host "[pylon] removed $p" }
    }
    return
}

$icon = "$Root\packaging\pylon.ico"
$shell = New-Object -ComObject WScript.Shell

foreach ($l in $links) {
    if (-not (Test-Path $l.Target)) {
        Write-Warning "no $($l.Target); skipping $($l.Name)"
        continue
    }
    $path = Join-Path $Desktop "$($l.Name).lnk"
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath = $l.Target
    # Without this a double-clicked shortcut runs from System32 and the .bat's
    # relative paths resolve to nothing.
    $sc.WorkingDirectory = $Root
    $sc.Description = $l.Desc
    if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
    $sc.Save()
    Write-Host "[pylon] created $path"
}

Write-Host ""
Write-Host "[pylon] Double-click Pylon on the Desktop to start the show."
Write-Host "[pylon] Then in OBS: View > Docks > Custom Browser Docks."
