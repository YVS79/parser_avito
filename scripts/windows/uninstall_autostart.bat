@echo off
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall_autostart.ps1" %*
pause
endlocal
