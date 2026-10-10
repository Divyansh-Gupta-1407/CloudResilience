# Overrides cart.html in the prebuilt frontend:v0.10.7 image with our fixed copy
# (December was labelled "January" in the expiry-month dropdown). Mounting a
# ConfigMap avoids rebuilding the image. Our cart.html == v0.10.7 + that one-line fix.
$ErrorActionPreference = 'Stop'
$ctx = 'kind-chaos-sandbox'
$ns = 'boutique'
$cart = Join-Path $PSScriptRoot '..\microservices-demo\src\frontend\templates\cart.html'

kubectl --context $ctx -n $ns create configmap frontend-template-patch --from-file=cart.html=$cart `
    --dry-run=client -o yaml | kubectl --context $ctx -n $ns apply -f -

$patch = @'
{"spec":{"template":{"spec":{
  "volumes":[{"name":"template-patch","configMap":{"name":"frontend-template-patch"}}],
  "containers":[{"name":"server","volumeMounts":[
    {"name":"template-patch","mountPath":"/src/templates/cart.html","subPath":"cart.html","readOnly":true}]}]
}}}}
'@
$tmp = New-TemporaryFile
Set-Content -Path $tmp -Value $patch -Encoding ascii
kubectl --context $ctx -n $ns patch deployment frontend --type strategic --patch-file $tmp
Remove-Item $tmp
kubectl --context $ctx -n $ns rollout status deployment/frontend --timeout=600s
