# Удаление автозапуска Avito Parser.
#
#   powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1 -Method Startup

[CmdletBinding()]
param(
    [ValidateSet("Task", "Startup")]
    [string]$Method = "Task",

    [string]$TaskName = "AvitoParser-4G",

    [switch]$StopRunning
)

$ErrorActionPreference = "Stop"

if ($Method -eq "Startup") {
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "AvitoParser 4G.lnk"
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath -Force
        Write-Host "Ярлык удалён: $ShortcutPath"
    } else {
        Write-Host "Ярлык не найден: $ShortcutPath"
    }
    return
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "Задача '$TaskName' не найдена."
    return
}

if ($StopRunning) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Задача '$TaskName' удалена."
