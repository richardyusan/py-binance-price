@echo off
rem Starts Binance Price Viewer with the Windows Python launcher (no console).
rem Use "run.bat --console" to keep a console window open for error output.
setlocal
cd /d "%~dp0"
if /i "%~1"=="--console" goto console

where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%~dp0binance_viewer.py"
    goto end
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0binance_viewer.py"
    goto end
)
goto notfound

:console
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0binance_viewer.py"
    goto end
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0binance_viewer.py"
    goto end
)

:notfound
echo Python 3 was not found. Install it from https://www.python.org/downloads/
echo (tick "py launcher" and "tcl/tk and IDLE" during setup), then run this again.
pause

:end
endlocal
