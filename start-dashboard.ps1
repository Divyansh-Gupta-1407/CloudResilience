# Starts everything for a demo, each in its own window (close a window to stop it):
#   shop on http://localhost:8080, backend API on http://127.0.0.1:8000 (also serves the built-in dashboard),
#   React frontend on http://localhost:5173 (opened in the browser).
#   .\start-dashboard.ps1              -> all of the above
#   .\start-dashboard.ps1 -Streamlit   -> also the older Streamlit dashboard at http://localhost:8501
# Prereqs: Docker Desktop running, cluster up (setup-cluster.ps1), Ollama running, Node.js installed.
param([switch]$Streamlit)
$root = $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
$fe = Join-Path $root 'frontend'

Start-Process powershell -WorkingDirectory $root -ArgumentList '-NoExit', '-File', "$root\open-shop.ps1"
Start-Process powershell -WorkingDirectory $root -ArgumentList '-NoExit', '-Command',
    "& '$py' -m uvicorn chaos.api:app --host 127.0.0.1 --port 8000"
if (-not (Test-Path (Join-Path $fe 'node_modules'))) {
    Write-Host 'Installing frontend packages (first run only)...'
    Push-Location $fe; npm install --no-audit --no-fund; Pop-Location
}
Start-Process powershell -WorkingDirectory $fe -ArgumentList '-NoExit', '-Command', 'npm run dev'
if ($Streamlit) {
    Start-Process powershell -WorkingDirectory $root -ArgumentList '-NoExit', '-Command',
        "& '$py' -m streamlit run dashboard\app.py --server.port 8501 --browser.gatherUsageStats false"
}

# Wait for the backend, start telemetry, open the frontend.
for ($i = 0; $i -lt 40; $i++) {
    try { Invoke-RestMethod http://127.0.0.1:8000/status -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 1 }
}
try { Invoke-RestMethod -Method Post http://127.0.0.1:8000/ingestion/start | Out-Null } catch {}
for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-WebRequest http://localhost:5173 -UseBasicParsing -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep 1 }
}
Start-Process http://localhost:5173
Write-Host 'ResilienceAI: http://localhost:5173'
Write-Host 'Shop:         http://localhost:8080'
Write-Host 'Backend API:  http://127.0.0.1:8000  (built-in dashboard; API docs at /docs)'
if ($Streamlit) { Write-Host 'Streamlit:    http://localhost:8501' }
