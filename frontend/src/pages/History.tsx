import { useState } from "react";
import type { ErrorEvent, EventRow, ExperimentRow, SquadRun } from "../api";
import { FAULT_LABEL } from "../api";
import { DataTable } from "../components/DataTable";
import { useLive, usePoll } from "../hooks";
import { Badge, Card, DataState, Mono, clock, cx, ms, pct, type Tone } from "../ui";

type Tab = "events" | "experiments" | "squads" | "errors";
const TABS: { id: Tab; label: string }[] = [
  { id: "events", label: "Events" }, { id: "experiments", label: "Experiments" },
  { id: "squads", label: "Squad runs" }, { id: "errors", label: "Errors" },
];
const CAT_TONE: Record<string, Tone> = { SAFETY: "bad", ERROR: "bad", RECOVERY: "ok", AI: "accent", EXPERIMENT: "warn", INFO: "idle" };

export function History() {
  const [tab, setTab] = useState<Tab>("events");
  return (
    <div className="flex flex-col gap-4">
      <div className="flex gap-1 rounded-lg border border-line bg-surface p-1 self-start">
        {TABS.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={cx("h-8 rounded-md px-4 text-[13px] font-medium", tab === t.id ? "bg-surface-2 text-ink" : "text-ink-3 hover:text-ink-2")}>
            {t.label}
          </button>
        ))}
      </div>
      <Card>
        {tab === "events" && <Events />}
        {tab === "experiments" && <Experiments />}
        {tab === "squads" && <Squads />}
        {tab === "errors" && <Errors />}
      </Card>
    </div>
  );
}

function Events() {
  const p = usePoll<EventRow[]>("/events?limit=500", 3000);
  return (
    <DataState loading={p.loading} error={p.error} hasData={!!p.data}>
      <DataTable<EventRow> filterable rows={p.data ?? []} empty="No events recorded yet" maxHeight={640} columns={[
        { key: "timestamp", header: "Time", render: r => <Mono className="text-ink-2">{clock(r.timestamp)}</Mono> },
        { key: "category", header: "Category", render: r => <Badge tone={CAT_TONE[r.category] ?? "idle"}>{r.category}</Badge> },
        { key: "event", header: "Event", render: r => <span className="text-[12.5px]">{r.event}</span> },
      ]} />
    </DataState>
  );
}

function Experiments() {
  const p = usePoll<ExperimentRow[]>("/experiments/history?limit=200", 5000);
  return (
    <DataState loading={p.loading} error={p.error} hasData={!!p.data}>
      <DataTable<ExperimentRow> filterable rows={p.data ?? []} empty="No experiments recorded yet" maxHeight={640} columns={[
        { key: "id", header: "Run", render: r => <Mono>{r.id}</Mono> },
        { key: "planned_by", header: "By" },
        { key: "target", header: "Target", render: r => <Mono>{r.target ?? "—"}</Mono> },
        { key: "fault", header: "Fault", render: r => FAULT_LABEL[r.fault] ?? r.fault ?? "—" },
        { key: "status", header: "Status", render: r => <Badge tone={r.status === "completed" ? "ok" : r.status === "frozen" ? "warn" : "idle"}>{r.status}</Badge> },
        { key: "stop_reason", header: "Stop reason", className: "text-ink-2" },
        { key: "error_rate_during", header: "Errors during", render: r => <Mono>{pct(r.error_rate_during)}</Mono> },
        { key: "p95_ms_during", header: "p95 during", render: r => <Mono>{ms(r.p95_ms_during)}</Mono> },
        { key: "recovered", header: "Recovered", render: r => r.recovered == null ? <span className="text-ink-3">—</span> : <Badge tone={r.recovered ? "ok" : "bad"}>{r.recovered ? "Yes" : "No"}</Badge>,
          sortValue: r => (r.recovered == null ? null : r.recovered ? 1 : 0) },
        { key: "recovery_time_s", header: "Recovery", render: r => <Mono>{r.recovery_time_s == null ? "—" : `${r.recovery_time_s} s`}</Mono> },
      ]} />
    </DataState>
  );
}

function Squads() {
  const p = usePoll<SquadRun[]>("/squad/history?limit=50", 5000);
  return (
    <DataState loading={p.loading} error={p.error} hasData={!!p.data}>
      <DataTable<SquadRun> filterable rows={p.data ?? []} empty="No squad runs yet" maxHeight={640} columns={[
        { key: "id", header: "Squad", render: r => <Mono>{r.id}</Mono> },
        { key: "status", header: "State", render: r => <Badge tone={r.status === "ROLLED BACK" ? "ok" : r.status === "FAILED" ? "bad" : "idle"}>{r.status}</Badge> },
        { key: "errors", header: "Errors injected", render: r => (
          <ul className="text-[12.5px]">{r.errors.map(e => <li key={e.agent}>{e.agent} <Mono className="text-ink-3">{e.model}</Mono> · {FAULT_LABEL[e.failure_type ?? ""] ?? "—"} on <Mono>{e.target ?? "—"}</Mono></li>)}</ul>),
          sortValue: r => r.errors.length },
        { key: "ranking", header: "Severity ranking", render: r => r.ranking.length
          ? <ul className="text-[12.5px]">{r.ranking.map(x => <li key={x.rank}>#{x.rank} <Mono>{x.target}</Mono> · {x.severity}</li>)}</ul>
          : <span className="text-ink-3">not analysed</span>, sortValue: r => r.ranking.length },
        { key: "analyst_model", header: "Analyst", render: r => <Mono>{r.analyst_model ?? "—"}</Mono> },
      ]} />
    </DataState>
  );
}

function Errors() {
  const { errors } = useLive();
  return (
    <DataState loading={errors.loading} error={errors.error} hasData={!!errors.data}>
      <DataTable<ErrorEvent> filterable rows={errors.data?.history ?? []} empty="No errors recorded yet" maxHeight={640} columns={[
        { key: "timestamp", header: "Time", render: r => <Mono className="text-ink-2">{clock(r.timestamp)}</Mono>, sortValue: r => r.t },
        { key: "service", header: "Service", render: r => <Mono>{r.service}</Mono> },
        { key: "type", header: "Type", render: r => <Badge tone={r.type.startsWith("Degraded") ? "idle" : r.type === "High latency" ? "warn" : "bad"}>{r.type}</Badge> },
        { key: "count", header: "Requests", render: r => <Mono>{r.count}</Mono> },
        { key: "attributed_by", header: "Attributed by", className: "text-ink-2" },
        { key: "message", header: "Message", render: r => <span className="text-xs text-ink-2">{r.message}</span> },
        { key: "suspected_cause", header: "Suspected cause", className: "text-ink-2" },
      ]} />
    </DataState>
  );
}
