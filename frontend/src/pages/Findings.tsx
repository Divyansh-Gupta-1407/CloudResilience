import { useState } from "react";
import { Download, Gauge, Hammer, RefreshCw, Search, ShieldAlert, Wrench, Zap } from "lucide-react";
import { FAULT_LABEL, post, type Finding, type Findings as FindingsT, type Severity } from "../api";
import { usePoll } from "../hooks";
import { Badge, Button, Card, DataState, Mono, cx, pct, useToast, type Tone } from "../ui";

const SEV_TONE: Record<Severity, Tone> = { Critical: "bad", High: "bad", Medium: "warn", Low: "ok" };
const SEV_BAR: Record<Severity, string> = { Critical: "bg-bad", High: "bg-bad/70", Medium: "bg-warn", Low: "bg-ok" };

export function Findings() {
  const f = usePoll<FindingsT>("/findings", 2000);
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const d = f.data;

  async function rerun() {
    setBusy(true);
    try { await post("/squad/analyze"); toast("Re-running severity ranking and fixes on the latest telemetry.", "ok"); }
    catch (e) { toast(`Analysis not started: ${(e as Error).message}`, "err"); }
    setBusy(false); f.refresh();
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-[320px_1fr] gap-4 max-[1200px]:grid-cols-1">
        <Card title="Resilience score" icon={<Gauge className="h-4 w-4" />}>
          <DataState loading={f.loading} error={f.error} hasData={!!d} empty={!!d && d.resilience_score == null}
            emptyText={d?.analysis_status === "running" ? "Analysis running…" : "No data: no squad run is active"}>
            {d && d.resilience_score != null && (
              <>
                <div className="flex items-end gap-2">
                  <Mono className={cx("text-5xl font-medium", d.resilience_score >= 70 ? "text-ok" : d.resilience_score >= 40 ? "text-warn" : "text-bad")}>{d.resilience_score}</Mono>
                  <span className="mb-1.5 text-ink-3">/ 100</span>
                  <Badge tone={d.resilience_score >= 70 ? "ok" : d.resilience_score >= 40 ? "warn" : "bad"} className="mb-2 ml-auto">
                    {d.resilience_score >= 70 ? "Resilient" : d.resilience_score >= 40 ? "Fragile" : "Not resilient"}
                  </Badge>
                </div>
                <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-surface-2">
                  <div className={cx("h-full", d.resilience_score >= 70 ? "bg-ok" : d.resilience_score >= 40 ? "bg-warn" : "bg-bad")} style={{ width: `${d.resilience_score}%` }} />
                </div>
                <p className="mt-3 text-xs leading-relaxed text-ink-3">{d.score_basis}</p>
              </>
            )}
          </DataState>
        </Card>
        <Card title="Run summary" icon={<ShieldAlert className="h-4 w-4" />}
          actions={<>
            <Button icon={<RefreshCw className="h-4 w-4" />} busy={busy} onClick={rerun} disabled={!d?.squad_id}>Re-run analysis</Button>
            <Button variant="primary" icon={<Download className="h-4 w-4" />} disabled={!d?.findings.length} onClick={() => exportReport(d!)}>Export report</Button>
          </>}>
          <DataState loading={f.loading} error={f.error} hasData={!!d}>
            <dl className="grid grid-cols-[160px_1fr] gap-y-2 text-[13px]">
              <dt className="text-ink-3">Squad run</dt><dd><Mono>{d?.squad_id ?? "no active run"}</Mono>{d?.previous_squad_id && <span className="ml-2 text-xs text-ink-3">previous: <Mono>{d.previous_squad_id}</Mono> (History)</span>}</dd>
              <dt className="text-ink-3">Telemetry</dt><dd><Badge tone={d?.ingestion === "RUNNING" ? "ok" : "idle"}>{d?.ingestion === "RUNNING" ? "Running" : "Stopped"}</Badge></dd>
              <dt className="text-ink-3">Squad state</dt><dd><Mono>{d?.status ?? "—"}</Mono></dd>
              <dt className="text-ink-3">Analysis</dt><dd><Mono>{d?.analysis_status ?? "not started"}</Mono></dd>
              <dt className="text-ink-3">Ranked by</dt><dd><Mono>{d?.analyst_model ?? "—"}</Mono>{d?.ranking_note && <span className="ml-2 text-xs text-warn">{d.ranking_note}</span>}</dd>
              <dt className="text-ink-3">Last rollback</dt>
              <dd>{d?.last_rollback
                ? d.last_rollback.recovery_verified
                  ? <Badge tone="ok">Recovery verified in {d.last_rollback.recovery_time_s} s</Badge>
                  : <Badge tone="bad">Recovery not verified</Badge>
                : <span className="text-ink-3">none since the backend started</span>}</dd>
            </dl>
          </DataState>
        </Card>
      </div>

      <DataState loading={f.loading} error={f.error} hasData={!!d} empty={!!d && d.findings.length === 0}
        emptyText={emptyText(d)}>
        <div className="flex flex-col gap-4">
          {d?.findings.map(x => <FindingCard key={x.agent} f={x} />)}
        </div>
      </DataState>
    </div>
  );
}

export function FindingCard({ f }: { f: Finding }) {
  const m = f.impact;
  return (
    <section className="rounded-xl border border-line bg-surface">
      <header className="flex items-center gap-4 border-b border-line px-4 py-3">
        <Mono className="text-2xl text-ink-2">#{f.rank}</Mono>
        <Badge tone={SEV_TONE[f.severity]}>{f.severity}</Badge>
        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-semibold"><Mono className="text-[14px] text-ink">{f.target}</Mono> <span className="text-ink-2 font-normal">· {FAULT_LABEL[f.failure_type] ?? f.failure_type}</span></div>
          <div className="text-xs text-ink-3">Injected by agent {f.agent} (<Mono>{f.agent_model}</Mono>) · ranked by <Mono>{f.ranked_by}</Mono></div>
        </div>
        <div className="w-44">
          <div className="flex justify-between text-[11px] text-ink-3"><span>formula score</span><Mono className="text-ink-2">{f.formula_score}/100</Mono></div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2"><div className={cx("h-full", SEV_BAR[f.severity])} style={{ width: `${f.formula_score}%` }} /></div>
        </div>
      </header>
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] gap-0 max-[1200px]:grid-cols-1">
        <div className="border-r border-line p-4 max-[1200px]:border-r-0 max-[1200px]:border-b">
          <h3 className="mb-2 text-[11.5px] font-semibold uppercase tracking-wider text-ink-3">Measured impact</h3>
          <div className="grid grid-cols-3 gap-2">
            <Stat label="Failed" value={`${m.failed_requests}/${m.requests_total}`} sub={pct(m.failure_rate)} tone={m.failed_requests ? "text-bad" : "text-ink"} />
            <Stat label="Slow" value={String(m.slow_requests)} sub={pct(m.slow_rate)} tone={m.slow_requests ? "text-warn" : "text-ink"} />
            <Stat label="Degraded" value={String(m.degraded_requests)} sub="worked with fallback" tone="text-ink" />
          </div>
          <dl className="mt-3 grid grid-cols-[110px_1fr] gap-y-1.5 text-[12.5px]">
            <dt className="text-ink-3">Pages broken</dt><dd>{m.pages_failed.join(", ") || "none"}</dd>
            <dt className="text-ink-3">Pages degraded</dt><dd>{m.pages_degraded.join(", ") || "none"}</dd>
            <dt className="text-ink-3">Suspected cause</dt><dd>{f.suspected_cause}</dd>
          </dl>
          <p className="mt-3 text-[12.5px] leading-relaxed text-ink-2">{f.justification}</p>
          {m.sample_messages.length > 0 && <Mono className="mt-2 block break-all text-[11px] text-ink-3">{m.sample_messages[0]}</Mono>}
        </div>
        <div className="p-4">
          <h3 className="mb-2 flex items-center gap-2 text-[11.5px] font-semibold uppercase tracking-wider text-ink-3">
            <Wrench className="h-3.5 w-3.5" />Recommended fix
            {f.fix && !f.fix.error && <span className="ml-auto font-normal normal-case tracking-normal">confidence <Mono>{f.fix.confidence}</Mono> · <Mono>{f.fix.model}</Mono></span>}
          </h3>
          {!f.fix ? <p className="text-[13px] text-ink-3">The analyst is writing this recommendation…</p>
            : f.fix.error ? <p className="text-[13px] text-bad">No recommendation: {f.fix.error}</p>
              : (
                <div className="grid grid-cols-2 gap-3 max-[1200px]:grid-cols-1">
                  <FixPart icon={<Search className="h-3.5 w-3.5" />} label="Likely cause" text={f.fix.likely_cause} />
                  <FixPart icon={<Zap className="h-3.5 w-3.5" />} label="Immediate fix" text={f.fix.immediate_fix} />
                  <FixPart icon={<Hammer className="h-3.5 w-3.5" />} label="Long-term fix" text={f.fix.long_term_fix} />
                  <FixPart icon={<ShieldAlert className="h-3.5 w-3.5" />} label="Prevention" text={f.fix.prevention} />
                </div>
              )}
        </div>
      </div>
    </section>
  );
}

function Stat({ label, value, sub, tone }: { label: string; value: string; sub: string; tone: string }) {
  return (
    <div className="rounded-lg border border-line bg-surface-2 px-3 py-2">
      <div className="text-[11px] text-ink-3">{label}</div>
      <Mono className={cx("text-lg", tone)}>{value}</Mono>
      <div className="text-[10.5px] text-ink-3">{sub}</div>
    </div>
  );
}

function FixPart({ icon, label, text }: { icon: React.ReactNode; label: string; text: string }) {
  return (
    <div className="rounded-lg border border-line bg-surface-2 p-3">
      <div className="mb-1 flex items-center gap-1.5 text-[11.5px] font-medium text-ink-2">{icon}{label}</div>
      <p className="text-[12.5px] leading-relaxed text-ink">{text}</p>
    </div>
  );
}

/** Markdown findings report, built only from the /findings response. */
function exportReport(d: FindingsT) {
  const lines = [
    `# ResilienceAI findings report`, ``,
    `- Squad run: \`${d.squad_id}\``, `- Generated: ${new Date().toISOString()}`,
    `- Resilience score: **${d.resilience_score ?? "n/a"} / 100** (${d.score_basis ?? ""})`,
    `- Ranked by: ${d.analyst_model ?? "n/a"}${d.ranking_note ? ` (${d.ranking_note})` : ""}`,
    `- Last rollback: ${d.last_rollback ? (d.last_rollback.recovery_verified ? `recovery verified in ${d.last_rollback.recovery_time_s} s` : "recovery NOT verified") : "none recorded"}`,
    ``, `| Rank | Severity | Service | Fault | Failed requests | Pages broken | Formula score |`, `|---|---|---|---|---|---|---|`,
    ...d.findings.map(f => `| ${f.rank} | ${f.severity} | ${f.target} | ${FAULT_LABEL[f.failure_type] ?? f.failure_type} | ${f.impact.failed_requests}/${f.impact.requests_total} (${pct(f.impact.failure_rate)}) | ${f.impact.pages_failed.join(", ") || "none"} | ${f.formula_score} |`),
    ``,
    ...d.findings.flatMap(f => [
      `## #${f.rank} ${f.severity}: ${f.target} (${FAULT_LABEL[f.failure_type] ?? f.failure_type})`, ``,
      `Injected by agent ${f.agent} (${f.agent_model}).`, ``,
      `**Measured impact:** ${f.impact.failed_requests} failed, ${f.impact.slow_requests} slow, ${f.impact.degraded_requests} degraded of ${f.impact.requests_total} requests. Pages broken: ${f.impact.pages_failed.join(", ") || "none"}. Pages degraded: ${f.impact.pages_degraded.join(", ") || "none"}.`, ``,
      `**Suspected cause:** ${f.suspected_cause}`, ``, `**Why this rank:** ${f.justification}`, ``,
      ...(f.fix && !f.fix.error ? [
        `**Recommended fix** (${f.fix.model}, confidence ${f.fix.confidence})`, ``,
        `- Likely cause: ${f.fix.likely_cause}`, `- Immediate fix: ${f.fix.immediate_fix}`,
        `- Long-term fix: ${f.fix.long_term_fix}`, `- Prevention: ${f.fix.prevention}`, ``,
      ] : [`**Recommended fix:** not available`, ``]),
    ]),
  ];
  const url = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/markdown" }));
  const a = document.createElement("a");
  a.href = url; a.download = `resilienceai-findings-${d.squad_id}.md`; a.click();
  URL.revokeObjectURL(url);
}

/** Severity ranking + recommendations in one panel (used on the Overview control center). */
export function SquadResults() {
  const f = usePoll<FindingsT>("/findings", 2000);
  const d = f.data;
  const score = d?.resilience_score;
  const waiting = emptyText(d);
  return (
    <Card title="Severity ranking and fix recommendations" icon={<Wrench className="h-4 w-4" />}
      actions={<>
        {score != null && <Badge tone={score >= 70 ? "ok" : score >= 40 ? "warn" : "bad"}>Resilience score {score}/100</Badge>}
        {d?.squad_id && <Mono className="text-ink-3">squad {d.squad_id}</Mono>}
        {d?.findings.length ? <Button icon={<Download className="h-4 w-4" />} onClick={() => exportReport(d)}>Export report</Button> : null}
      </>}>
      <DataState loading={f.loading} error={f.error} hasData={!!d} empty={!!d && d.findings.length === 0} emptyText={waiting}>
        {d && d.findings.length > 0 && (
          <>
            <ol className="mb-4 grid grid-cols-3 gap-3 max-[1200px]:grid-cols-1">
              {d.findings.map(x => (
                <li key={x.agent} className="flex items-center gap-3 rounded-lg border border-line bg-surface-2 px-3 py-2.5">
                  <Mono className="text-xl text-ink-2">#{x.rank}</Mono>
                  <Badge tone={SEV_TONE[x.severity]}>{x.severity}</Badge>
                  <div className="min-w-0">
                    <Mono className="block truncate text-ink">{x.target}</Mono>
                    <div className="text-[11.5px] text-ink-3">{FAULT_LABEL[x.failure_type] ?? x.failure_type} · {x.impact.failed_requests}/{x.impact.requests_total} failed</div>
                  </div>
                </li>
              ))}
            </ol>
            {d.ranking_note && <p className="mb-3 text-xs text-warn">{d.ranking_note}</p>}
            <div className="flex flex-col gap-4">{d.findings.map(x => <FindingCard key={x.agent} f={x} />)}</div>
            {d.analysis_status === "running" && <p className="mt-3 text-xs text-ink-3">Updating…</p>}
          </>
        )}
      </DataState>
    </Card>
  );
}


/** What to show while there are no findings, depending on where the run is. */
function emptyText(d: FindingsT | null): string {
  if (!d) return "No data";
  if (!d.active)
    return "No active run. Findings appear only for the run in progress: launch the 3 agents (telemetry starts automatically). Past runs are listed under History.";
  if (d.analysis_error) return `Analysis failed: ${d.analysis_error}`;
  if (d.analysis_status === "running") return "The analyst is ranking the errors and writing fix recommendations…";
  if (d.status === "PLANNING") return "The agents are choosing their errors…";
  if (d.status === "INJECTING") return "Injecting the errors…";
  return "Errors are active. The severity ranking and fix recommendations appear about 30 s after injection.";
}