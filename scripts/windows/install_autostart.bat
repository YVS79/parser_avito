@echo off
chcp 65001 >nul
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_autostart.ps1" %*
if errorlevel 1 (
    echo.
    echo Не удалось установить автозапуск. Смотрите сообщение выше.
)
pause
endlocal
