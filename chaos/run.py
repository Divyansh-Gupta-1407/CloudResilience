"""CLI.

  python -m chaos.run baseline [--seconds 60]
  python -m chaos.run experiment --type service-down --target cartservice --duration 20 [--hypothesis "..."]
  python -m chaos.run plan      # AI ranks experiments; every proposal is validated, nothing runs
  python -m chaos.run auto      # AI plans, then the controller runs its single top-ranked proposal
  python -m chaos.run random    # one random fault on a random service (20 s, auto-stops on breach)
  python -m chaos.run stop      # kill switch: stops the running process, freezing the current state
  python -m chaos.run rollback  # restore the initial state (undo every fault left in place)
  python -m chaos.run clear     # remove the kill switch so new experiments may run
"""
import argparse
import json

from .controller import capture_baseline, kill_switch_path, load_safety, run_experiment


def main():
    ap = argparse.ArgumentParser(prog="chaos.run")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("--seconds", type=int, default=60)
    e = sub.add_parser("experiment")
    e.add_argument("--type", required=True)
    e.add_argument("--target", required=True)
    e.add_argument("--duration", type=float, required=True)
    e.add_argument("--hypothesis", default="")
    e.add_argument("--delay-ms", type=int, help="network-delay only: added latency in ms")
    sub.add_parser("plan")
    a = sub.add_parser("auto")
    a.add_argument("--top", type=int, default=1, choices=[1])
    sub.add_parser("random")
    sub.add_parser("stop")
    sub.add_parser("rollback")
    sub.add_parser("clear")
    args = ap.parse_args()

    if args.cmd == "random":
        import random
        from .faults import CATALOG
        cfg = load_safety()
        target, ftype = random.choice(cfg["allowed_targets"]), random.choice(sorted(CATALOG))
        params = {"delay_ms": random.choice([200, 300, 500, 800, 1000, 1500])} if ftype == "network-delay" else {}
        print(f"random choice: {ftype} on {target} {params or ''}")
        rec = run_experiment({"failure_type": ftype, "target": target, "params": params, "duration_s": 20,
                              "hypothesis": "random error injection", "reason": "chosen at random"})
        print(json.dumps({k: rec.get(k) for k in ("run_id", "status", "stop_reason", "during_fault", "recovery")},
                         indent=2))
        return

    if args.cmd == "rollback":
        import yaml
        from .discovery import DEFAULT_MANIFEST
        from .faults import Kube, reset_all
        cfg = load_safety()
        docs = [d for d in yaml.safe_load_all(open(DEFAULT_MANIFEST)) if d]
        desired = {d["metadata"]["name"]: d["spec"].get("replicas", 1) for d in docs if d.get("kind") == "Deployment"}
        actions = reset_all(Kube(cfg["cluster_context"], cfg["namespace"]), cfg["allowed_targets"], desired)
        kill_switch_path(cfg).unlink(missing_ok=True)
        print("\n".join(actions) or "already in the initial state - nothing to undo")
        return

    if args.cmd in ("plan", "auto"):
        from .planner import make_plan, run_plan
        plan = make_plan()
        _print_plan(plan)
        if args.cmd == "plan" or plan["status"] != "ok":
            return
        run_plan(plan, top=args.top,
                 on_start=lambda p: print(f"\n=== running rank {p['rank']}: {p['failure_type']} on {p['target']} ==="),
                 on_result=lambda p, rec: print(f"    -> {rec.get('status')} / {rec.get('stop_reason')} / "
                                                f"recovery={rec.get('recovery')}"))
        if kill_switch_path(load_safety()).exists():
            print("kill switch is ON - automatic run stopped")
        return

    if args.cmd == "baseline":
        print(json.dumps(capture_baseline(args.seconds), indent=2))
    elif args.cmd == "experiment":
        params = {"delay_ms": args.delay_ms} if args.delay_ms is not None else {}
        rec = run_experiment({"failure_type": args.type, "target": args.target, "params": params,
                              "duration_s": args.duration, "hypothesis": args.hypothesis})
        summary = {k: rec.get(k) for k in ("run_id", "status", "reason", "stop_reason", "cleanup_status",
                                           "pre_fault", "during_fault", "recovery")}
        print(json.dumps(summary, indent=2))
    elif args.cmd in ("stop", "clear"):
        path = kill_switch_path(load_safety())
        if args.cmd == "stop":
            path.parent.mkdir(exist_ok=True)
            path.write_text("stop")
            print(f"kill switch ON ({path})")
        else:
            path.unlink(missing_ok=True)
            print("kill switch cleared")


def _print_plan(plan):
    print(f"plan {plan['plan_id']}  model={plan['model']}  status={plan['status']}")
    if plan["status"] != "ok":
        print(f"  MODEL UNAVAILABLE: {plan['error']}")
        print("  Nothing will be executed.")
        return
    for p in plan["proposals"]:
        mark = "OK " if p["verdict"] == "accepted" else "REJ"
        extra = f" +{p['params']['delay_ms']}ms" if p.get("params", {}).get("delay_ms") else ""
        print(f"  [{mark}] #{p['rank']} {p['failure_type']:<13} {p['target']:<22} {p['duration_s']}s{extra}")
        print(f"        why: {p['reason']}")
        print(f"        expect: {p['hypothesis']}")
        if p["verdict"] == "rejected":
            print(f"        REJECTED: {p['rejection']}")


if __name__ == "__main__":
    main()
