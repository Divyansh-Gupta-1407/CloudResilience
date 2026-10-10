"""Switch the shop's frontend between Google's original image and our resilient build.

  python patches/use_frontend.py resilient   # our fixed build (graceful degradation, timeouts, fast reconnect)
  python patches/use_frontend.py original    # Google's v0.10.7 image + the December label patch

The resilient image is built from microservices-demo/src/frontend (see the "Resilience fix"
comments there) and loaded into the kind node, so no registry is needed.
"""
import json
import subprocess
import sys

CTX, NS, CLUSTER = "kind-chaos-sandbox", "boutique", "chaos-sandbox"
ORIGINAL = "us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/frontend:v0.10.7"
RESILIENT = "frontend-resilient:v1"
PATCH_VOLUME = "template-patch"


def run(*cmd, input=None):
    p = subprocess.run(cmd, capture_output=True, text=True, input=input)
    if p.returncode != 0:
        sys.exit(f"{' '.join(cmd)} failed:\n{p.stderr}")
    return p.stdout


def kubectl(*args, input=None):
    return run("kubectl", "--context", CTX, "-n", NS, *args, input=input)


def main(mode):
    dep = json.loads(kubectl("get", "deployment", "frontend", "-o", "json"))
    spec = dep["spec"]["template"]["spec"]
    container = next(c for c in spec["containers"] if c["name"] == "server")
    volumes = [v for v in spec.get("volumes", []) if v["name"] != PATCH_VOLUME]
    mounts = [m for m in container.get("volumeMounts", []) if m["name"] != PATCH_VOLUME]

    if mode == "resilient":
        print("loading image into the kind node...")
        run("kind", "load", "docker-image", RESILIENT, "--name", CLUSTER)
        image, policy = RESILIENT, "IfNotPresent"       # templates (incl. December fix) are baked in
    else:
        image, policy = ORIGINAL, "IfNotPresent"
        volumes.append({"name": PATCH_VOLUME, "configMap": {"name": "frontend-template-patch"}})
        mounts.append({"name": PATCH_VOLUME, "mountPath": "/src/templates/cart.html",
                       "subPath": "cart.html", "readOnly": True})

    ops = [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": image},
           {"op": "replace", "path": "/spec/template/spec/containers/0/imagePullPolicy", "value": policy},
           {"op": "add", "path": "/spec/template/spec/volumes", "value": volumes},
           {"op": "add", "path": "/spec/template/spec/containers/0/volumeMounts", "value": mounts}]
    assert spec["containers"][0]["name"] == "server"
    kubectl("patch", "deployment", "frontend", "--type", "json", "-p", json.dumps(ops))
    print(kubectl("rollout", "status", "deployment/frontend", "--timeout=180s").strip())
    print(f"frontend now runs: {image}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("resilient", "original"):
        sys.exit(__doc__)
    main(sys.argv[1])
