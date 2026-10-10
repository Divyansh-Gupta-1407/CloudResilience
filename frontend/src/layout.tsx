import { useState } from "react";
import { Activity, FlaskConical, History, LayoutDashboard, OctagonX, RotateCcw, ShieldCheck, FileSearch } from "lucide-react";
import { API_URL, post, type RollbackResult } from "./api";
import { useLive } from "./hooks";
import { Badge, Button, ConfirmDialog, cx, useToast } from "./ui";

export type Page = "overview" | "experiments" | "findings" | "history";
export const PAGES: { id: Page; label: string; icon: typeof Activity }[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "experiments", label: "Experiments", icon: FlaskConical },
  { id: "findings", label: "Findings", icon: FileSearch },
  { id: "history", label: "History", icon: History },
];

export function Sidebar({ page, onNavigate }: { page: Page; onNavigate: (p: Page) => void }) {
  const { status } = useLive();
  const s = status.data;
  return (
    <aside className="flex w-[232px] shrink-0 flex-col border-r border-line bg-surface">
      <div className="flex items-center gap-2.5 px-5 py-5">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent/15 text-accent"><ShieldCheck className="h-[18px] w-[18px]" /></div>
        <div>
          <div className="text-[15px] font-semibold leading-tight">ResilienceAI</div>
          <div className="text-[11.5px] text-ink-3">Chaos control center</div>
        </div>
      </div>
      <nav className="flex flex-col gap-0.5 px-3">
        {PAGES.map(p => (
          <button key={p.id} onClick={() => onNavigate(p.id)}
            className={cx("flex h-9 items-center gap-2.5 rounded-lg px-3 text-[13px] font-medium transition-colors",
              page === p.id ? "bg-accent/10 text-accent" : "text-ink-2 hover:bg-surface-2 hover:text-ink")}>
            <p.icon className="h-4 w-4" />{p.label}
          </button>
        ))}
      </nav>
      <div className="mt-auto p-3">
        <div className="rounded-xl border border-line bg-surface-2 p-3.5 text-xs">
          <div className="mb-2 font-semibold text-ink-2 uppercase tracking-wider text-[10.5px]">Environment</div>
          <Row k="App" v="Online Boutique" />
          <Row k="Cluster" v="kind-chaos-sandbox" mono />
          <Row k="Namespace" v="boutique" mono />
          <Row k="Telemetry" v={s ? <Badge tone={s.ingestion === "RUNNING" ? "ok" : "idle"}>{s.ingestion === "RUNNING" ? "Running" : "Stopped"}</Badge> : "—"} />
          <Row k="Planner" v={s?.ai_backend ?? "—"} mono />
        </div>
      </div>
    </aside>
  );
}

function Row({ k, v, mono }: { k: string; v: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2 py-1">
      <span className="text-ink-3">{k}</span>
      <span className={cx("text-right text-ink", mono && "font-mono text-[11.5px]")}>{v}</span>
    </div>
  );
}

/** Kill switch + rollback actions with their confirmation dialogs (used by the top bar and the Experiments page). */
export function useControlActions() {
  const { refreshAll } = useLive();
  const toast = useToast();
  const [dialog, setDialog] = useState<"kill" | "rollback" | null>(null);
  const [rollingBack, setRollingBack] = useState(false);

  async function kill() {
    setDialog(null);
    try {
      await post("/experiment/stop");
      toast("Kill switch on. The agents stop and the system is frozen in its current state. Roll back to restore it.", "ok");
    } catch (e) { toast(`Kill switch failed: ${(e as Error).message}`, "err"); }
    refreshAll();
  }

  async function rollback() {
    setDialog(null);
    setRollingBack(true);
    toast("Rolling back to the initial state and verifying recovery. This can take up to two minutes.", "info");
    try {
      const r = await post<RollbackResult>("/rollback", { confirm: true }, 300000);
      if (r.recovery_verified)
        toast(`Recovery verified ${r.recovery_time_s} s after the restore. ${r.actions.length} change(s) reverted.`, "ok");
      else
        toast(`Rollback finished, but recovery is not verified.${r.not_ready.length ? ` Not ready: ${r.not_ready.join(", ")}.` : ""} ${r.note ?? ""}`, "err");
    } catch (e) { toast(`Rollback failed: ${(e as Error).message}`, "err"); }
    setRollingBack(false);
    refreshAll();
  }

  const dialogs = (
    <>
      <ConfirmDialog open={dialog === "kill"} title="Activate the kill switch?"
        body={<>Stops every running agent and experiment immediately and <b>freezes the system in its current state</b>. Injected errors stay in place until you roll back.</>}
        confirmLabel="Freeze now" onConfirm={kill} onCancel={() => setDialog(null)} />
      <ConfirmDialog open={dialog === "rollback"} title="Roll back to the initial state?" tone="primary"
        body={<>Stops anything running, restores every service's replicas, removes injected network delay and ends crash loops, then verifies recovery with live telemetry. History is kept.</>}
        confirmLabel="Roll back" onConfirm={rollback} onCancel={() => setDialog(null)} />
    </>
  );
  return { askKill: () => setDialog("kill"), askRollback: () => setDialog("rollback"), rollingBack, dialogs };
}

/** Top bar: page title, connection state, the always-visible kill switch and rollback. */
export function TopBar({ title }: { title: string }) {
  const { status } = useLive();
  const { askKill, askRollback, rollingBack, dialogs } = useControlActions();
  const connected = !!status.data && !status.error;
  const faults = status.data?.active_faults.length ?? 0;

  return (
    <header className="flex h-16 shrink-0 items-center justify-between gap-4 border-b border-line bg-bg/80 px-8 backdrop-blur">
      <div className="flex items-center gap-4">
        <h1 className="text-lg font-semibold">{title}</h1>
        {status.data && <Badge tone={faults ? "bad" : status.data.experiment_status === "IDLE" ? "ok" : "warn"}>{status.data.experiment_status}</Badge>}
      </div>
      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2 text-xs text-ink-2" title={API_URL}>
          <span className={cx("h-2 w-2 rounded-full", connected ? "bg-ok" : status.loading ? "bg-idle" : "bg-bad")} />
          {connected ? "Backend connected" : status.loading ? "Connecting…" : "Backend unreachable"}
        </div>
        <Button variant="secondary" icon={<RotateCcw className="h-4 w-4" />} busy={rollingBack} onClick={askRollback} disabled={!connected}>
          Roll back
        </Button>
        <Button variant="danger" icon={<OctagonX className="h-4 w-4" />} onClick={askKill} disabled={!connected}>
          Kill switch
        </Button>
      </div>
      {dialogs}
    </header>
  );
}
