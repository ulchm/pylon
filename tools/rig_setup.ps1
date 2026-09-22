<#
Set Pylon up on a Windows PC that ALREADY runs another broadcaster, without
touching it. Written for the author's own sim rig, where the GOW broadcaster
(`iracing_broadcaster`, ports 8777-8782) is the live show and must not be
disturbed by a test.

    powershell -NoProfile -ExecutionPolicy Bypass -File tools\rig_setup.ps1

What it does, and why each part:

  * clones (or updates) into %USERPROFILE%\Code\pylon, well away from the other
    checkout, then `uv sync` into its own environment;
  * writes a config.toml that moves every port into the 887x range, so even
    starting both broadcasters by mistake cannot collide. Pylon only ever
    reclaims a port from a process whose command line says "pylon", and the
    other one only matches "irbcast", so neither can kill the other's workers,
    but a clean failure is still a failure and this avoids it entirely;
  * gives the show a NAME, which is what keeps the OBS scenes apart: every
    scene Pylon builds carries it, the programme scene included, so `obs-setup`
    can never reach into the live show's scenes;
  * runs the test suite, which is the gate. A green suite on the rig is what
    catches the environment-dependent failures a Linux run cannot.

It does not start anything. Nothing here touches the sim, so it is safe to run
over SSH; starting the studio is a console-session job.
#>
param(
    [string]$Repo = "git@github.com:ulchm/pylon.git",
    [string]$Bundle = "",                 # use a .git bundle instead of GitHub
    [string]$Dest = "$env:USERPROFILE\Code\pylon",
    [string]$ShowName = "Pylon Test",
    [string]$ShowTag = "PylonTest",
    [switch]$SkipTests,
    [switch]$NoShortcut
)

$ErrorActionPreference = "Stop"

function Step($msg) { Write-Host "[pylon] $msg" -ForegroundColor Cyan }

# --- 1. the checkout ---------------------------------------------------------
if (Test-Path "$Dest\.git") {
    Step "updating the existing checkout at $Dest"
    if ($Bundle) {
        git -C $Dest pull $Bundle main
    } else {
        git -C $Dest pull origin main
    }
} else {
    Step "cloning into $Dest"
    New-Item -ItemType Directory -Force -Path (Split-Path $Dest) | Out-Null
    if ($Bundle) {
        git clone $Bundle $Dest
        # A bundle clone's "origin" is the file, which will not exist next time.
        git -C $Dest remote set-url origin $Repo
    } else {
        git clone $Repo $Dest
    }
}

# --- 2. the environment ------------------------------------------------------
Step "syncing the environment (uv)"
Push-Location $Dest
try {
    uv sync

    # --- 3. the settings, written to keep out of the other broadcaster's way ---
    # %LOCALAPPDATA%\Pylon, not the checkout: an upgrade must not take them with it.
    $cfgDir = "$env:LOCALAPPDATA\Pylon"
    $cfgPath = "$cfgDir\config.toml"
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null

    if (Test-Path $cfgPath) {
        Step "settings already exist at $cfgPath, leaving them alone"
    } else {
        Step "writing settings to $cfgPath"
        @"
# Pylon on the sim rig, set up to coexist with the other broadcaster.
#
# The ports are moved into the 887x range so that starting both by mistake
# cannot collide: the other show owns 8777-8782.
#
# The show NAME is what keeps the OBS scenes apart. Every scene Pylon builds
# carries it, so `obs-setup` here makes "$ShowTag - Broadcast" and leaves the
# live show's "iRacing - Broadcast" untouched.

[show]
name = "$ShowName"
tag = "$ShowTag"
round = ""
subtitle = ""

[look]
colour = "#38BDF8"
logo = ""
tower = true
bug = true

[obs]
host = "localhost"
port = 4455
password = ""
scenes = true
transition = "Fade"
# Left empty on purpose: with no destination set, Pylon does not touch OBS's
# encoder settings, which live in the PROFILE and are shared with the other show.
stream_server = ""
stream_key = ""

[director]
replays = true
angles = "tv"
favourites = []
min_shot = 6.0

[advanced]
bridge_port = 8879
overlay_http_port = 8878
overlay_ws_port = 8877
control_port = 8882
"@ | Set-Content -Path $cfgPath -Encoding ascii   # NOT UTF8: PowerShell 5 writes a BOM
    }

    # --- 4. the gate -----------------------------------------------------------
    if (-not $SkipTests) {
        Step "running the test suite (the gate)"
        uv run pytest -q -p no:cacheprovider
        if ($LASTEXITCODE -ne 0) { throw "the suite failed on this box; stopping here" }
    }

    Step "checking the setup"
    uv run python -m pylon doctor --no-sim
} finally {
    Pop-Location
}

# --- 5. OBS's own settings ---------------------------------------------------
# obs-websocket ships DISABLED, and nothing can talk to OBS until it is on, so this
# cannot be done over the WebSocket. It edits OBS's config files instead, which
# means OBS has to be closed; the command refuses and says so if it is not.
Step "setting OBS up (needs OBS closed)"
Push-Location $Dest
try { uv run python -m pylon obs-prepare } finally { Pop-Location }

# --- 6. the Desktop shortcut -------------------------------------------------
# Starting a broadcast should be a double-click, not a remembered command.
if (-not $NoShortcut) {
    Step "putting Pylon on the Desktop"
    powershell -NoProfile -ExecutionPolicy Bypass -File "$Dest\tools\make_shortcut.ps1" -Root $Dest
}

Write-Host ""
Step "ready. Double-click PYLON on the Desktop, from the console session."
Step "(or, in a terminal there:)"
Write-Host "    cd $Dest"
Write-Host "    uv run pylon studio"
Write-Host ""
Step "then in OBS add a Custom Browser Dock pointing at:"
Write-Host "    http://127.0.0.1:8882/"
Write-Host ""
Step "the sim's shared memory is per-session, so the bridge can only see iRacing"
Step "from the console session. Over SSH it will start and read nothing."
