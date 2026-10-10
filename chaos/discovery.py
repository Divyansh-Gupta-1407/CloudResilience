"""App discovery: build a service dependency map from Kubernetes manifests.

Edges come from *_ADDR env vars ("host:port") on each Deployment. Manual
corrections (extra edges, criticality, journeys) come from overrides.yaml.

Usage: python -m chaos.discovery [manifest.yaml] [--out out/dependency_map]
"""
import argparse
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "microservices-demo" / "release" / "kubernetes-manifests.yaml"
DEFAULT_OVERRIDES = Path(__file__).with_name("overrides.yaml")


def _containers(deployment):
    spec = deployment["spec"]["template"]["spec"]
    return spec.get("containers", []) + spec.get("initContainers", [])


def discover(manifest_path, overrides_path=DEFAULT_OVERRIDES):
    docs = [d for d in yaml.safe_load_all(Path(manifest_path).read_text()) if d]
    services = {}
    edges = []

    for d in docs:
        if d.get("kind") != "Deployment":
            continue
        name = d["metadata"]["name"]
        ports = [p["containerPort"] for c in d["spec"]["template"]["spec"]["containers"]
                 for p in c.get("ports", [])]
        services[name] = {"name": name, "ports": ports, "deployed": True}
        seen = set()
        for c in _containers(d):
            for env in c.get("env", []) or []:
                if not env["name"].endswith("_ADDR") or "value" not in env:
                    continue
                host, _, port = env["value"].partition(":")
                if (host, port) in seen:
                    continue
                seen.add((host, port))
                edges.append({"from": name, "to": host, "port": int(port) if port else None,
                              "via": env["name"], "source": "manifest"})

    # Targets referenced but not deployed (e.g. optional shoppingassistantservice).
    for e in edges:
        if e["to"] not in services:
            services[e["to"]] = {"name": e["to"], "ports": [], "deployed": False}

    overrides = {}
    if overrides_path and Path(overrides_path).exists():
        overrides = yaml.safe_load(Path(overrides_path).read_text()) or {}
    for e in overrides.get("extra_edges", []):
        edges.append({**e, "source": "manual"})
    for name, meta in (overrides.get("services") or {}).items():
        services.setdefault(name, {"name": name, "ports": [], "deployed": False}).update(meta)

    for s in services.values():
        s["callers"] = sorted({e["from"] for e in edges if e["to"] == s["name"]})
        s["calls"] = sorted({e["to"] for e in edges if e["from"] == s["name"]})

    return {
        "manifest": str(Path(manifest_path).relative_to(ROOT)) if Path(manifest_path).is_relative_to(ROOT) else str(manifest_path),
        "services": dict(sorted(services.items())),
        "edges": edges,
        "journeys": overrides.get("journeys", {}),
    }


def to_mermaid(dep_map):
    lines = ["graph LR"]
    for s in dep_map["services"].values():
        label = s["name"] + ("" if s["deployed"] else " (not deployed)")
        style = ":::critical" if s.get("critical") else ""
        lines.append(f'  {s["name"]}["{label}"]{style}')
    for e in dep_map["edges"]:
        arrow = "-.->" if e["source"] == "manual" else "-->"
        lines.append(f'  {e["from"]} {arrow} {e["to"]}')
    lines.append("  classDef critical stroke:#d00,stroke-width:3px")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", nargs="?", default=DEFAULT_MANIFEST)
    ap.add_argument("--overrides", default=DEFAULT_OVERRIDES)
    ap.add_argument("--out", default=ROOT / "out" / "dependency_map")
    args = ap.parse_args()

    dep_map = discover(args.manifest, args.overrides)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(dep_map, indent=2))
    out.with_suffix(".mmd").write_text(to_mermaid(dep_map))

    print(f"{len(dep_map['services'])} services, {len(dep_map['edges'])} edges")
    for s in dep_map["services"].values():
        flag = " [critical]" if s.get("critical") else ""
        flag += "" if s["deployed"] else " [not deployed]"
        print(f"  {s['name']:<26} -> {', '.join(s['calls']) or '-'}{flag}")
    print(f"wrote {out.with_suffix('.json')} and {out.with_suffix('.mmd')}")


if __name__ == "__main__":
    main()
