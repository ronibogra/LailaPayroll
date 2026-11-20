# Called by the installer: registers LailaPayroll to start with Windows (as SYSTEM), starts it,
# and waits until it answers. Output is written to C:\ProgramData\LailaPayroll\logs\install.log
param([string]$App = $PSScriptRoot, [int]$Port = 8585)
$logs = Join-Path $env:ProgramData 'LailaPayroll\logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
Start-Transcript -Path (Join-Path $logs 'install.log') -Append | Out-Null
try {
    $a = New-ScheduledTaskAction -Execute (Join-Path $App 'LailaPayroll.exe') -WorkingDirectory $App
    $t = New-ScheduledTaskTrigger -AtStartup
    $s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
         -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName 'LailaPayroll' -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null
    Write-Output "Task registered"
    Start-ScheduledTask -TaskName 'LailaPayroll'
    for ($i = 0; $i -lt 90; $i++) {
        try {
            Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5 | Out-Null
            Write-Output "Server answered after $i s"
            break
        } catch { Start-Sleep -Seconds 1 }
    }
} catch {
    Write-Output "ERROR: $_"
} finally {
    Stop-Transcript | Out-Null
}
