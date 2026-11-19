# Автозапуск Avito Parser вместе с Windows.
#
# Примеры:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -Mode Gui -StartNow
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1 -Method Startup
#
# По умолчанию создаётся задача Планировщика задач при входе пользователя:
# парсер запускается без консольного окна через pythonw.exe.

[CmdletBinding()]
param(
    [ValidateSet("Headless", "Gui")]
    [string]$Mode = "Headless",

    [ValidateSet("Task", "Startup")]
    [string]$Method = "Task",

    [string]$TaskName = "AvitoParser-4G",

    [int]$DelaySeconds = 30,

    [switch]$StartNow
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path $Root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $Python)) {
    $command = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($command) {
        $Python = $command.Source
    }
}

if (-not (Test-Path $Python)) {
    throw @"
pythonw.exe не найден.
Сначала создайте окружение и установите зависимости:
  cd "$Root"
  run_avito_parser.bat
или:
  python -m venv .venv
  .venv\Scripts\python -m pip install -r requirements.txt
"@
}

if ($Mode -eq "Gui") {
    $Target = Join-Path $Root "AvitoParser.py"
    $Arguments = "`"$Target`""
} else {
    $Target = Join-Path $Root "scripts\windows\run_headless.py"
    $Arguments = "`"$Target`""
}

if (-not (Test-Path $Target)) {
    throw "Не найден файл для запуска: $Target"
}

$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

Write-Host "Проект:      $Root"
Write-Host "Python:      $Python"
Write-Host "Режим:       $Mode"
Write-Host "Способ:      $Method"
Write-Host "Пользователь: $Identity"

if ($Method -eq "Startup") {
    $StartupDir = [Environment]::GetFolderPath("Startup")
    $ShortcutPath = Join-Path $StartupDir "AvitoParser 4G.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = $Python
    $Shortcut.Arguments = $Arguments
    $Shortcut.WorkingDirectory = $Root
    if ($Mode -eq "Gui") { $Shortcut.WindowStyle = 1 } else { $Shortcut.WindowStyle = 7 }
    $Shortcut.Description = "Avito Parser 4G (autostart)"
    $Shortcut.Save()

    Write-Host "Ярлык создан: $ShortcutPath"
    Write-Host "Задержка $DelaySeconds сек. в этом способе не применяется."
    if ($StartNow) {
        Start-Process -FilePath $Python -ArgumentList $Arguments -WorkingDirectory $Root
        Write-Host "Парсер запущен."
    }
    return
}

$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
if ($DelaySeconds -gt 0) {
    $Trigger.Delay = "PT${DelaySeconds}S"
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Avito Parser 4G: автозапуск при входе пользователя ($Mode)" `
    -Force | Out-Null

Write-Host "Задача '$TaskName' создана (запуск через $DelaySeconds сек. после входа)."
Write-Host "Проверить:  Get-ScheduledTask -TaskName '$TaskName'"
Write-Host "Логи:       $Root\logs\headless.log и $Root\logs\app.log"

if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Задача запущена."
}
