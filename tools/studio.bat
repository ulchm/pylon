@echo off
REM One-click launcher for a SOURCE CHECKOUT: double-click this, then add the control
REM panel to OBS. People who installed Pylon do not need it; they have a shortcut.
REM
REM Deliberately runs python.exe and NOT pythonw.exe. A console window here is a
REM feature for a solo operator: it carries the supervision log, which is the only
REM place a worker that will not start explains itself. The workers get no windows of
REM their own: the supervisor spawns them with CREATE_NO_WINDOW.
REM
REM The pause at the end matters more than it looks. Without it, anything that fails
REM during startup closes the window instantly and takes the traceback with it, which
REM is the single most confusing way for a double-clicked launcher to behave.

setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [studio] no environment here. Run:  uv sync
  echo [studio] in: %CD%
  goto :done
)

.venv\Scripts\python.exe -m pylon studio %*

:done
echo.
echo [studio] exited. Press any key to close.
pause >nul
