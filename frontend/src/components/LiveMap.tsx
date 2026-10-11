import { useState } from "react";
import { ChevronDown, ChevronUp, Network } from "lucide-react";
import { useLive, useNow } from "../hooks";
import { Badge, DataState, Mono, mmss, type Tone } from "../ui";
import { DependencyGraph, HEALTH_STYLE } from "./DependencyGraph";
import { cx } from "../ui";

const KEY = "resilienceai.mapOpen";
const readOpen = () => { try { return localStorage.getItem(KEY) !== "0"; } catch { return true; } };
const saveOpen = (v: boolean) => { try { localStorage.setItem(KEY, v ? "1" : "0"); } catch { /* storage unavailable */ } };

/** The service dependency map, shown above every page so the run can be followed from anywhere. */
export function LiveMap() {
  const { services, status, errors, squad } = useLive();
  const now = useNow();
  const [open, setOpen] = useState(readOpen);
  const s = status.data, q = squad.data;
  const faults = s?.active_faults ?? [];
  const agents = q?.active ? q.agents : [];
  const thinking = agents.find(a => a.status === "thinking");

  // One line saying what is happening right now.
  let tone: Tone = "ok", text = "Idle: all services in their initial state";
  if (q?.active && q.status === "PLANNING") {
    tone = "warn";
    text = thinking ? `Agent ${thinking.id} (${thinking.model}) is choosing a ${thinking.name.toLowerCase().replace(" agent", "")} target`
      : q.note ?? "Agents are choosing their errors";
  } else if (q?.active && q.status === "INJECTING") { tone = "warn"; text = "Injecting the chosen errors"; }
  else if (faults.length && q?.active && q.status === "HOLDING") {
    tone = "bad"; text = `${faults.length} error(s) active for ${q.injected_at ? mmss(now / 1000 - q.injected_at) : "—"}`
      + (q.analysis_status === "running" ? " · analyst ranking" : q.analysis_status === "done" ? " · ranking ready" : "");
  } else if (faults.length && s?.experiment_status.startsWith("FROZEN")) { tone = "bad"; text = `Frozen by kill switch: ${faults.length} error(s) left in place, roll back to restore`; }
  else if (faults.length) { tone = "bad"; text = `${faults.length} fault(s) active (${s?.experiment_status})`; }
  else if (s && s.experiment_status !== "IDLE") { tone = "warn"; text = s.experiment_status; }
  if (s?.guardrail?.rolling_back) { tone = "warn"; text = "Rolling back to the initial state and verifying recovery"; }
  else if (s?.guardrail && s.guardrail.breaching_for_s > 0) {
    tone = "bad"; text = `Shop down for ${s.guardrail.breaching_for_s} s: automatic rollback at ${s.guardrail.duration_s} s`;
  }

  return (
    <section className="mb-6 rounded-xl border border-line bg-surface">
      <header className="flex items-center justify-between gap-4 px-4 py-2.5">
        <button className="flex items-center gap-2 text-[13px] font-semibold" onClick={() => { setOpen(!open); saveOpen(!open); }}>
          <Network className="h-4 w-4 text-ink-2" />Live service dependency map
          {open ? <ChevronUp className="h-4 w-4 text-ink-3" /> : <ChevronDown className="h-4 w-4 text-ink-3" />}
        </button>
        <div className="flex min-w-0 flex-1 items-center justify-end gap-3">
          <Badge tone={tone} className="max-w-[520px]"><span className="truncate" title={text}>{text}</span></Badge>
          {open && (
            <div className="flex items-center gap-3 text-[11px] text-ink-3 max-[1300px]:hidden">
              {(["healthy", "chosen", "fault", "affected"] as const).map(h => (
                <span key={h} className="flex items-center gap-1.5"><span className={cx("h-2 w-2 rounded-full", HEALTH_STYLE[h].dot)} />{HEALTH_STYLE[h].label}</span>
              ))}
            </div>
          )}
        </div>
      </header>
      {open && (
        <div className="border-t border-line p-2">
          <DataState loading={services.loading} error={services.error} hasData={!!services.data}
            empty={services.data?.length === 0} emptyText="No services reported by the cluster">
            <DependencyGraph services={services.data ?? []} faults={faults} errors={errors.data?.by_service ?? []} agents={agents} height={380} />
          </DataState>
          <p className="px-2 pb-1 text-[11px] text-ink-3">
            Boxes are the shop's deployments (<Mono className="text-[11px]">ready/desired</Mono> pods); arrows show who calls whom.
            Red = error injected (animated arrows lead into it), amber = chosen by an agent or producing errors, with live failed / slow / degraded counts from the last 15 s.
          </p>
        </div>
      )}
    </section>
  );
}
