@echo off
REM Check the setup and HOLD THE WINDOW OPEN, which is the whole point of having
REM this as a shortcut: a doctor whose output vanishes has told nobody anything.

setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [pylon] no environment here. Run:  uv sync
  echo [pylon] in: %CD%
  goto :done
)

.venv\Scripts\python.exe -m pylon doctor %*

:done
echo.
echo Press any key to close.
pause >nul
