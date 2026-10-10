# Keeps http://localhost:8080 connected to the shop. kubectl port-forward binds to a
# single pod and exits when that pod dies (e.g. during a frontend experiment), so we
# restart it in a loop. Leave this window open; Ctrl+C to stop.
$ctx = 'kind-chaos-sandbox'
if (Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue) {
    Write-Host 'Port 8080 is already in use - the shop is probably already open in another window.'
    Write-Host 'Just browse to http://localhost:8080, or close the other window and run this again.'
    return
}
Write-Host 'Shop: http://localhost:8080  (Ctrl+C to stop)'
while ($true) {
    kubectl --context $ctx -n boutique port-forward svc/frontend 8080:80 | Out-Null
    Write-Host "$(Get-Date -Format HH:mm:ss) connection lost (frontend restarting?) - reconnecting..."
    Start-Sleep -Seconds 2
}
