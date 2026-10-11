# Stops the hidden ResilienceAI backend started by start-dashboard.ps1.
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object CommandLine -match 'uvicorn chaos.api' |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "stopped backend (pid $($_.ProcessId))" }
