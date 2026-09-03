# Registers a Scheduled Task so the bot starts at logon and restarts on failure.
# Run once from a normal (non-admin) PowerShell window:
#   powershell -ExecutionPolicy Bypass -File .\install-autostart.ps1
#
# To remove later:
#   Unregister-ScheduledTask -TaskName FotoHu -Confirm:$false

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$taskName = 'FotoHu'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'

if (-not (Test-Path $pythonw)) { throw "not found: $pythonw - create .venv first" }

$action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m fotohu' -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host 'Old task removed.'
}

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'FotoHu - family photo archiver' | Out-Null
Write-Host "Task '$taskName' created."

Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 8
$info = Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo
$state = (Get-ScheduledTask -TaskName $taskName).State
Write-Host ("State: {0}, LastTaskResult: 0x{1:X}" -f $state, $info.LastTaskResult)
