# Keeps the Teach Me web site running: starts `electron . --server` from Task Scheduler
# and restarts it if it stops.
#
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1            install (or update)
#   powershell -ExecutionPolicy Bypass -File scripts\autostart.ps1 -Remove    remove
#
# Run from an Administrator PowerShell and the site starts at boot, before anyone signs in.
# Run normally and it starts when this Windows user signs in.
param([switch]$Remove)

$TaskName = "Teach Me web site"
$Electron = Join-Path $PSScriptRoot "..\electron" | Resolve-Path
$Exe = Join-Path $Electron "node_modules\electron\dist\electron.exe"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Removed the '$TaskName' task. A copy that is already running keeps running until it is closed."
    return
}

if (-not (Test-Path $Exe)) { throw "Electron not found at $Exe. Run 'npm install' in $Electron first." }

$user = [Security.Principal.WindowsIdentity]::GetCurrent()
$admin = (New-Object Security.Principal.WindowsPrincipal $user).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

$action = New-ScheduledTaskAction -Execute $Exe -Argument ". --server" -WorkingDirectory $Electron
# No time limit (the default stops a task after 3 days). Task Scheduler doesn't restart a program that
# crashes, so a second trigger tries every 5 minutes; while the site runs, IgnoreNew makes that a no-op.
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5)

if ($admin) {
    # At boot, as this user, without storing a password (S4U: local files only, which is all it needs).
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $principal = New-ScheduledTaskPrincipal -UserId $user.Name -LogonType S4U -RunLevel Limited
    $when = "at boot, before anyone signs in"
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user.Name
    $principal = New-ScheduledTaskPrincipal -UserId $user.Name -LogonType Interactive -RunLevel Limited
    $when = "when $($user.Name) signs in (run this as Administrator to start it at boot instead)"
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($trigger, $watchdog) -Principal $principal `
    -Settings $settings -Description "Teach Me web site on http://127.0.0.1:8795 (for the Cloudflare tunnel)" `
    -Force | Out-Null
Write-Output "Installed '$TaskName': starts $when, and again within 5 minutes if it stops."
