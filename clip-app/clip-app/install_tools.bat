@echo off
echo Installing Python and FFmpeg (already-installed items are skipped)...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
echo.
echo Done. CLOSE this window, open a NEW one, then run install_deps.bat
pause
