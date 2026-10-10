import { Activity, AlertOctagon, Gauge, Server, TrendingUp } from "lucide-react";
import type { ReactNode } from "react";
import type { ServiceErrors } from "../api";
import { ErrorRateChart, LatencyChart } from "../components/Charts";
import { DataTable } from "../components/DataTable";
import { useLive } from "../hooks";
import { CurrentExperiment, Ingestion, ManualExperiment, RandomInjection, Squad } from "./Experiments";
import { SquadResults } from "./Findings";
import { Card, DataState, Mono, clock, cx, ms, pct, type Tone } from "../ui";

export function Overview() {
  const { status, metrics, errors } = useLive();
  const m = metrics.data, e = errors.data, s = status.data;
  const base = m?.baseline ?? s?.baseline ?? null;
  const last = m ? [...m.rows].reverse().find(r => r.n) ?? null : null;
  const noTelemetry = s?.ingestion === "STOPPED";
  const kpiError = metrics.error && !m ? metrics.error : null;

  // Expected failures in the same 15 s window at the baseline error rate.
  const expectedFailed = base && e ? base.error_rate * e.total_requests : null;

  return (
    <div className="flex flex-col gap-6">
      {noTelemetry && (
        <div className="rounded-lg border border-warn/40 bg-warn/10 px-4 py-2.5 text-[13px] text-warn">
          Telemetry ingestion is stopped, so no live measurements are coming in. Press Start in the Telemetry ingestion panel below.
        </div>
      )}

      <div className="grid grid-cols-4 gap-4 max-[1200px]:grid-cols-2">
        <Kpi icon={<AlertOctagon className="h-4 w-4" />} label="Active errors" hint={e ? `failed requests, last ${e.window_s} s` : undefined}
          value={e ? String(e.failed_requests) : null} error={errors.error && !e ? errors.error : null} loading={errors.loading}
          tone={e ? (e.failed_requests ? "bad" : "ok") : "idle"}
          delta={e && expectedFailed != null ? `${signed(e.failed_requests - expectedFailed, 0)} vs baseline (~${expectedFailed.toFixed(0)})` : "no baseline"}
          extra={e && (e.slow_requests || e.degraded_requests) ? `${e.slow_requests} slow · ${e.degraded_requests} degraded of ${e.total_requests}` : e ? `of ${e.total_requests} requests` : undefined} />
        <Kpi icon={<Server className="h-4 w-4" />} label="Affected services" hint="last 15 s"
          value={e ? String(e.affected_services.length) : null} error={errors.error && !e ? errors.error : null} loading={errors.loading}
          tone={e ? (e.affected_services.length ? "bad" : "ok") : "idle"}
          delta={e ? `${signed(e.affected_services.length, 0)} vs baseline (0)` : undefined}
          extra={e?.affected_services.join(", ") || (e ? "none" : undefined)} />
        <Kpi icon={<Gauge className="h-4 w-4" />} label="p95 latency" hint="last 3 s window"
          value={last ? ms(last.p95_ms) : null} error={kpiError} loading={metrics.loading}
          tone={last ? ((last.p95_ms ?? 0) > 250 || last.stalled ? "bad" : (last.p95_ms ?? 0) > 100 ? "warn" : "ok") : "idle"}
          delta={last && base ? `${signed((last.p95_ms ?? 0) - base.p95_ms, 0)} ms vs baseline (${Math.round(base.p95_ms)} ms)` : "no baseline"}
          extra={last?.stalled ? `${last.stalled} request(s) hanging now` : undefined} />
        <Kpi icon={<TrendingUp className="h-4 w-4" />} label="Error rate" hint="last 3 s window"
          value={last ? pct(last.error_rate) : null} error={kpiError} loading={metrics.loading}
          tone={last ? ((last.error_rate ?? 0) > 0.1 ? "bad" : (last.error_rate ?? 0) > 0.02 ? "warn" : "ok") : "idle"}
          delta={last && base ? `${signed(100 * ((last.error_rate ?? 0) - base.error_rate), 1)} pp vs baseline (${pct(base.error_rate)})` : "no baseline"}
          extra={last ? `${last.n} requests in window` : undefined} />
      </div>

      <div className="grid grid-cols-2 gap-4 max-[1200px]:grid-cols-1">
        <CurrentExperiment />
        <div className="flex flex-col gap-4">
          <Ingestion />
          <RandomInjection />
        </div>
      </div>

      <Squad />

      <SquadResults />

      <ManualExperiment />

      <div className="grid grid-cols-2 gap-4 max-[1200px]:grid-cols-1">
        <Card title="p95 latency" icon={<Activity className="h-4 w-4" />} actions={<Legend />}>
          <DataState loading={metrics.loading} error={metrics.error} hasData={!!m} empty={!!m && m.rows.every(r => !r.n)}
            emptyText={noTelemetry ? "No data: telemetry ingestion is stopped" : "No samples yet"}>
            <LatencyChart rows={m?.rows ?? []} baseline={base?.p95_ms ?? null} />
          </DataState>
        </Card>
        <Card title="Error rate by user journey" icon={<TrendingUp className="h-4 w-4" />} actions={<Legend />}>
          <DataState loading={metrics.loading} error={metrics.error} hasData={!!m} empty={!!m && m.rows.every(r => !r.n)}
            emptyText={noTelemetry ? "No data: telemetry ingestion is stopped" : "No samples yet"}>
            <ErrorRateChart rows={m?.rows ?? []} baseline={base?.error_rate ?? null} />
          </DataState>
        </Card>
      </div>

      <Card title="Affected services" icon={<Server className="h-4 w-4" />} actions={<span className="text-xs text-ink-3">last 15 s</span>}>
        <DataState loading={errors.loading} error={errors.error} hasData={!!e} empty={!!e && e.by_service.length === 0}
          emptyText="No service is producing errors right now">
          <DataTable<ServiceErrors> rows={e?.by_service ?? []} initialSort={{ key: "failed_requests", dir: "desc" }} columns={[
            { key: "service", header: "Service", render: r => <Mono className="text-ink">{r.service}</Mono> },
            { key: "failed_requests", header: "Failed", render: r => <Mono className={r.failed_requests ? "text-bad" : ""}>{r.failed_requests}</Mono> },
            { key: "slow_requests", header: "Slow", render: r => <Mono className={r.slow_requests ? "text-warn" : ""}>{r.slow_requests}</Mono> },
            { key: "degraded_requests", header: "Degraded", render: r => <Mono>{r.degraded_requests}</Mono> },
            { key: "types", header: "Error types" },
            { key: "suspected_cause", header: "Suspected cause", className: "text-ink-2" },
            { key: "last_message", header: "Latest error", render: r => <span className="text-xs text-ink-2">{r.last_message}</span> },
            { key: "last_seen", header: "Last seen", render: r => <Mono className="text-ink-2">{clock(r.last_seen)}</Mono> },
          ]} />
        </DataState>
      </Card>
    </div>
  );
}

function Legend() {
  return (
    <div className="flex items-center gap-3 text-[11px] text-ink-3">
      <span className="flex items-center gap-1.5"><span className="inline-block h-0 w-4 border-t border-dashed border-ink-2" />baseline</span>
      <span className="flex items-center gap-1.5"><span className="inline-block h-2.5 w-3 rounded-sm bg-bad/20 border border-bad/40" />fault active</span>
    </div>
  );
}

const signed = (x: number, digits: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x).toFixed(digits)}`;

function Kpi({ icon, label, hint, value, tone, delta, extra, loading, error }: {
  icon: ReactNode; label: string; hint?: string; value: string | null; tone: Tone; delta?: string; extra?: string;
  loading: boolean; error: string | null;
}) {
  const color = { ok: "text-ok", warn: "text-warn", bad: "text-bad", idle: "text-ink-2", accent: "text-accent" }[tone];
  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      <div className="flex items-center justify-between text-[12.5px] text-ink-2">
        <span className="flex items-center gap-2">{icon}{label}</span>
        {hint && <span className="text-[11px] text-ink-3">{hint}</span>}
      </div>
      <div className={cx("mt-2 font-mono text-[28px] font-medium leading-none", value == null ? "text-ink-3" : color)}>
        {value ?? (loading ? "…" : "—")}
      </div>
      <div className="mt-2 font-mono text-[11.5px] text-ink-2">{error ? <span className="text-bad">{error}</span> : value == null && !loading ? "No data" : delta}</div>
      {extra && <div className="mt-1 truncate text-[11.5px] text-ink-3" title={extra}>{extra}</div>}
    </div>
  );
}
