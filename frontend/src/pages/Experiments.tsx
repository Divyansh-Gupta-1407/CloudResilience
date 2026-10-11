import { useState } from "react";
import { Bot, Brain, Dices, FlaskConical, OctagonX, Pause, Play, Radio, RotateCcw, Rocket, Square, Timer } from "lucide-react";
import { FAULT_LABEL, SERVICES, post, type Agent } from "../api";
import { useLive, useNow, usePoll } from "../hooks";
import { useControlActions } from "../layout";
import { SquadResults } from "./Findings";
import { Badge, Button, Card, DataState, Mono, cx, mmss, useToast, type Tone } from "../ui";

const FORM_FAULTS = [
  { value: "service_unavailability", label: "Service unavailability (outage)" },
  { value: "pod_failure", label: "Pod failure (crash loop)" },
  { value: "latency", label: "Latency injection (slow network)" },
];

export function Experiments() {
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-[1fr_1fr] gap-4 max-[1200px]:grid-cols-1">
        <CurrentExperiment />
        <div className="flex flex-col gap-4">
          <Ingestion />
          <RandomInjection />
        </div>
      </div>
      <Squad />
      <SquadResults />
      <ManualExperiment />
    </div>
  );
}

// ---------------------------------------------------------------- current state

export function CurrentExperiment() {
  const { status, squad } = useLive();
  const { askKill, askRollback, rollingBack, dialogs } = useControlActions();
  const now = useNow();
  const s = status.data, q = squad.data;
  const faults = s?.active_faults ?? [];
  const cur = s?.current_experiment;
  const squadActive = q && ["PLANNING", "INJECTING", "HOLDING"].includes(q.status);
  const since = q?.injected_at && squadActive ? q.injected_at : s?.job.started && s.job.status === "RUNNING" ? s.job.started : null;
  const st = s?.experiment_status ?? "—";
  const tone: Tone = faults.length || st.startsWith("FROZEN") ? "bad" : st === "IDLE" ? "ok" : "warn";

  return (
    <Card title="Current experiment" icon={<FlaskConical className="h-4 w-4" />}>
      <DataState loading={status.loading} error={status.error} hasData={!!s}>
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-xs text-ink-3">State</div>
            <div className="mt-1"><Badge tone={tone} className="text-[13px]">{st}</Badge></div>
          </div>
          <div className="text-right">
            <div className="flex items-center justify-end gap-1.5 text-xs text-ink-3"><Timer className="h-3.5 w-3.5" />Active for</div>
            <Mono className="mt-1 block text-xl text-ink">{since ? mmss(now / 1000 - since) : "—"}</Mono>
            <div className="text-[11px] text-ink-3">held until rollback · 10 min safety backstop</div>
          </div>
        </div>
        <dl className="mt-4 grid grid-cols-[120px_1fr] gap-y-1.5 text-[13px]">
          <dt className="text-ink-3">ID</dt>
          <dd><Mono>{cur?.run_id ?? (squadActive ? `squad ${q?.id}` : s?.job.id ?? "—")}</Mono></dd>
          <dt className="text-ink-3">Job</dt>
          <dd><Mono>{s?.job.kind ?? "—"} · {s?.job.status ?? "—"}</Mono>{s?.job.error && <span className="ml-2 text-bad text-xs">{s.job.error}</span>}</dd>
          {cur && (<><dt className="text-ink-3">Fault</dt>
            <dd>{FAULT_LABEL[cur.failure_type] ?? cur.failure_type} on <Mono>{cur.target}</Mono>{cur.params?.delay_ms ? <Mono> (+{cur.params.delay_ms} ms)</Mono> : null}</dd></>)}
        </dl>
        <div className="mt-4">
          <div className="mb-2 text-xs text-ink-3">Faults in the cluster right now</div>
          {faults.length === 0 ? (
            <div className="rounded-lg border border-line bg-surface-2 px-3 py-2.5 text-[13px] text-ink-2">None. The system is in its initial state.</div>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {faults.map(f => (
                <li key={f.service + f.fault} className="flex items-center justify-between rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-[13px]">
                  <span><Mono className="text-ink">{f.service}</Mono> <span className="text-ink-2">· {f.fault}</span></span>
                  <Mono className="text-ink-2">{f.detail}</Mono>
                </li>
              ))}
            </ul>
          )}
        </div>
        {s?.guardrail && <Guardrail g={s.guardrail} />}
        <div className="mt-4 grid grid-cols-2 gap-2">
          <Button variant="danger" icon={<OctagonX className="h-4 w-4" />} onClick={askKill}>Stop (freeze state)</Button>
          <Button variant="secondary" icon={<RotateCcw className="h-4 w-4" />} busy={rollingBack} onClick={askRollback}>Roll back</Button>
        </div>
      </DataState>
      {dialogs}
    </Card>
  );
}

// ---------------------------------------------------------------- ingestion

export function Ingestion() {
  const { status, refreshAll } = useLive();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const running = status.data?.ingestion === "RUNNING";
  async function act(path: string, ok: string) {
    setBusy(true);
    try { await post(path); toast(ok, "ok"); } catch (e) { toast(`Failed: ${(e as Error).message}`, "err"); }
    setBusy(false); refreshAll();
  }
  return (
    <Card title="Telemetry ingestion" icon={<Radio className="h-4 w-4" />}
      actions={status.data && <Badge tone={running ? "ok" : "idle"}>{running ? "Running" : "Stopped"}</Badge>}>
      <p className="text-[13px] text-ink-2">Steady synthetic shopper traffic through the shop; every request is measured in 3-second windows.</p>
      <div className="mt-3 flex gap-2">
        <Button variant="primary" icon={<Play className="h-4 w-4" />} busy={busy} disabled={running || !status.data}
          onClick={() => act("/ingestion/start", "Ingestion started")}>Start</Button>
        <Button icon={<Square className="h-4 w-4" />} disabled={!running || busy}
          onClick={() => act("/ingestion/stop", "Ingestion stopped. History is kept.")}>Stop</Button>
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- random injection

export function RandomInjection() {
  const { refreshAll } = useLive();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [last, setLast] = useState<string | null>(null);
  async function inject() {
    setBusy(true);
    try {
      const r = await post<{ chosen: { target: string; failure_type: string; params: { delay_ms?: number } } }>("/experiments/random-errors", { hold: true });
      const c = r.chosen, txt = `${FAULT_LABEL[c.failure_type] ?? c.failure_type} on ${c.target}${c.params.delay_ms ? ` (+${c.params.delay_ms} ms)` : ""}`;
      setLast(txt);
      toast(`Random pick: ${txt}. Injecting after the health check (about 18 s).`, "ok");
    } catch (e) { toast(`Random injection refused: ${(e as Error).message}`, "err"); }
    setBusy(false); refreshAll();
  }
  return (
    <Card title="Random error injection" icon={<Dices className="h-4 w-4" />}>
      <p className="text-[13px] text-ink-2">One fault, chosen at random (service, fault type and delay). Still validated by the safety controller.</p>
      <div className="mt-3 flex items-center gap-3">
        <Button icon={<Dices className="h-4 w-4" />} busy={busy} onClick={inject}>Inject 1 random error</Button>
        {last && <span className="text-xs text-ink-2">Last pick: <Mono>{last}</Mono></span>}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------- AI squad (replaces the ranked experiment list)

const AGENT_TONE: Record<string, Tone> = {
  waiting: "idle", thinking: "warn", chosen: "accent", holding: "bad", frozen: "bad", failed: "bad",
  "rolled back": "ok", "removed (backstop)": "ok",
};

export function Squad() {
  const { squad, refreshAll } = useLive();
  const toast = useToast();
  const now = useNow();
  const [busy, setBusy] = useState(false);
  const q = squad.data;
  const running = !!q && ["PLANNING", "INJECTING", "HOLDING"].includes(q.status);

  async function launch() {
    setBusy(true);
    try {
      await post("/squad/launch");
      toast("Squad launched. The three agents are choosing their errors (about 1 to 2 minutes).", "ok");
    } catch (e) { toast(`Launch refused: ${(e as Error).message}`, "err"); }
    setBusy(false); refreshAll();
  }

  const analysisIn = q?.injected_at && q.status === "HOLDING" && !q.analysis_status ? Math.max(0, q.observe_s - (now / 1000 - q.injected_at)) : null;

  return (
    <Card title="AI chaos squad" icon={<Bot className="h-4 w-4" />}
      actions={<>
        {q && <Badge tone={q.status === "HOLDING" || q.status === "FROZEN" ? "bad" : running ? "warn" : "idle"}>{q.active ? q.status : "READY"}</Badge>}
        <Button variant="primary" icon={<Rocket className="h-4 w-4" />} busy={busy} disabled={running} onClick={launch}>Launch 3 agents</Button>
      </>}>
      <p className="text-[13px] text-ink-2">
        Three agents on three different models. On every launch the three error types (outage, crash loop, network latency) are
        shuffled between them, and each agent chooses its target from its own random shortlist of services, so every run differs.
        They choose one after another, then all three errors are injected together and held until rollback. Telemetry starts
        automatically if it is off. After {q?.observe_s ?? 30} s of live measurements the analyst model ranks the errors by severity
        and writes fix recommendations.
      </p>
      <DataState loading={squad.loading} error={squad.error} hasData={!!q}>
        <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-ink-2">
          <span>Squad <Mono className="text-ink">{q?.active ? q.id : "no active run"}</Mono></span>
          {!q?.active && q?.id && <span>Previous run <Mono className="text-ink-2">{q.id}</Mono> · {q.status.toLowerCase()} (see History)</span>}
          <span className="flex items-center gap-1.5"><Brain className="h-3.5 w-3.5" />Analyst <Mono className="text-ink">{q?.analyst.model ?? q?.analyst.preferred}</Mono></span>
          {q?.analyst.note && <span className="text-warn">{q.analyst.note}</span>}
          {analysisIn != null && <span>Measuring <Mono className="text-ink">{q?.measured_requests ?? 0}/{q?.min_requests ?? 150}</Mono> requests · analysis in <Mono className="text-ink">{mmss(analysisIn)}</Mono> or when enough data</span>}
          {q?.active && q.analysis_status && <span>Analysis <Mono className="text-ink">{q.analysis_status}</Mono></span>}
          {q?.active && q.analysis_error && <span className="text-bad">{q.analysis_error}</span>}
          {q?.note && <span className="text-warn">{q.note}</span>}
        </div>
        {q && <GraphProgress q={q} />}
        <div className="mt-4 grid grid-cols-3 gap-4 max-[1200px]:grid-cols-1">
          {(q?.active && q.agents.length ? q.agents : PLACEHOLDER).map(a => <AgentCard key={a.id} a={a} idle={!(q?.active && q.agents.length)} />)}
        </div>
      </DataState>
    </Card>
  );
}

// Roster shown when no run is active (models only; roles and targets are decided at launch).
const PLACEHOLDER: Agent[] = [
  { id: "A", model: "qwen3:4b" }, { id: "B", model: "ministral-3:3b" }, { id: "C", model: "llama3.2:3b" },
].map(a => ({ ...a, name: "Agent", fault: "", status: "waiting", thinking: "", pick: null, error: null, fallback: false, applied: false }));

function AgentCard({ a, idle }: { a: Agent; idle: boolean }) {
  return (
    <div className="flex flex-col rounded-xl border border-line bg-surface-2 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-[13.5px] font-semibold">Agent {a.id}{a.fault ? ` · ${a.name}` : ""}</div>
          <Mono className="text-ink-3">{a.model}</Mono>
        </div>
        <Badge tone={idle ? "idle" : AGENT_TONE[a.status] ?? "idle"}>{idle ? "ready" : a.status}</Badge>
      </div>
      <div className="mt-3 text-xs text-ink-3">Fault type</div>
      <div className="text-[13px]">{a.fault ? FAULT_LABEL[a.fault] ?? a.fault : <span className="text-ink-3">Assigned at random on launch</span>}</div>
      {a.shortlist && a.shortlist.length > 0 && (
        <>
          <div className="mt-3 text-xs text-ink-3">Random shortlist</div>
          <div className="flex flex-wrap gap-1">{a.shortlist.map(s => <Mono key={s} className={cx("rounded border border-line px-1.5 py-0.5 text-[11px]", a.pick?.target === s ? "border-accent/60 text-accent" : "text-ink-2")}>{s}</Mono>)}</div>
        </>
      )}
      <div className="mt-3 text-xs text-ink-3">Chosen target</div>
      {a.pick ? (
        <>
          <div className="text-[13px]"><Mono className="text-ink">{a.pick.target}</Mono>{a.pick.params.delay_ms ? <Mono className="text-ink-2"> +{a.pick.params.delay_ms} ms</Mono> : null}
            {a.fallback && <Badge tone="warn" className="ml-2">random fallback</Badge>}</div>
          <p className="mt-2 text-xs leading-relaxed text-ink-2">{a.pick.reason}</p>
        </>
      ) : <div className="text-[13px] text-ink-3">{idle ? "Chosen after launch" : a.status === "thinking" ? "Choosing…" : "Not chosen yet"}</div>}
      {a.pick && a.recalled?.[a.pick.target] && (
        <p className="mt-2 text-[11.5px] text-ink-3">
          Memory: {a.recalled[a.pick.target].times_tested
            ? `tested ${a.recalled[a.pick.target].times_tested}x before, worst score ${a.recalled[a.pick.target].worst?.score ?? "—"} (${a.recalled[a.pick.target].worst?.severity ?? "—"})`
            : "never tested before"}
        </p>
      )}
      {a.message && <p className="mt-2 rounded-md border border-accent/30 bg-accent/5 px-2 py-1.5 text-[11.5px] text-ink-2"><span className="text-accent">To team:</span> {a.message}</p>}
      {a.error && <p className="mt-2 text-xs text-warn">{a.error}</p>}
      {a.status === "thinking" && a.thinking && (
        <pre className={cx("mt-3 max-h-36 overflow-auto whitespace-pre-wrap rounded-lg border border-line bg-bg p-2.5 font-mono text-[11px] leading-relaxed text-ink-2")}>
          {a.thinking.slice(-900)}
        </pre>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- manual experiment

export function ManualExperiment() {
  const { refreshAll } = useLive();
  const toast = useToast();
  const [target, setTarget] = useState("cartservice");
  const [fault, setFault] = useState("service_unavailability");
  const [delay, setDelay] = useState(500);
  const [objective, setObjective] = useState("Observe service degradation and recovery");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    try {
      const r = await post<{ id: string }>("/experiments", {
        target_service: target, fault_type: fault, hold: true, objective,
        ...(fault === "latency" ? { delay_ms: delay } : {}),
      });
      toast(`Experiment ${r.id} queued. The fault is injected after the health check (about 18 s).`, "ok");
      setAck(false);
    } catch (e) { toast(`Experiment refused: ${(e as Error).message}`, "err"); }
    setBusy(false); refreshAll();
  }

  const field = "h-9 w-full rounded-lg border border-line bg-surface-2 px-3 text-[13px] outline-none focus:border-accent/60";
  return (
    <Card title="Manual experiment" icon={<FlaskConical className="h-4 w-4" />}>
      <div className="grid grid-cols-4 gap-4 max-[1200px]:grid-cols-2">
        <label className="flex flex-col gap-1.5 text-xs text-ink-2">Target service
          <select className={field} value={target} onChange={e => setTarget(e.target.value)}>
            {SERVICES.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label className="flex flex-col gap-1.5 text-xs text-ink-2">Fault type
          <select className={field} value={fault} onChange={e => setFault(e.target.value)}>
            {FORM_FAULTS.map(f => <option key={f.value} value={f.value}>{f.label}</option>)}
          </select>
        </label>
        <label className={cx("flex flex-col gap-1.5 text-xs text-ink-2", fault !== "latency" && "opacity-40")}>
          Added latency <Mono className="text-ink">{delay} ms</Mono>
          <input type="range" min={50} max={3000} step={50} value={delay} disabled={fault !== "latency"}
            onChange={e => setDelay(+e.target.value)} className="h-9 accent-[#14b8a6]" />
        </label>
        <label className="flex flex-col gap-1.5 text-xs text-ink-2">Objective
          <input className={field} value={objective} onChange={e => setObjective(e.target.value)} />
        </label>
      </div>
      {target === "frontend" && <p className="mt-3 text-xs text-warn">frontend is the entry point: a fault there takes the whole shop offline.</p>}
      <div className="mt-4 flex items-center justify-between gap-4">
        <label className="flex items-center gap-2 text-[13px] text-ink-2">
          <input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)} className="accent-[#14b8a6]" />
          I understand this experiment may affect availability.
        </label>
        <div className="flex items-center gap-3">
          <span className="text-xs text-ink-3">Held until rollback or kill switch (10 min backstop)</span>
          <Button variant="primary" icon={<Play className="h-4 w-4" />} disabled={!ack} busy={busy} onClick={start}>Start experiment</Button>
        </div>
      </div>
    </Card>
  );
}


function Guardrail({ g }: { g: NonNullable<import("../api").Status["guardrail"]> }) {
  const breaching = g.breaching_for_s > 0;
  return (
    <div className={cx("mt-4 rounded-lg border px-3 py-2.5 text-[12.5px]", breaching ? "border-bad/50 bg-bad/10" : "border-line bg-surface-2")}>
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-ink">Emergency auto-rollback guardrail</span>
        <Badge tone={g.rolling_back ? "warn" : breaching ? "bad" : g.armed ? "accent" : "idle"}>
          {g.rolling_back ? "Rolling back" : breaching ? `Shop down ${g.breaching_for_s}/${g.duration_s} s` : g.armed ? "Armed" : "Standby"}
        </Badge>
      </div>
      <p className="mt-1 text-ink-3">
        Rolls back automatically if {Math.round(100 * g.error_rate)}% of shopper requests (or {Math.round(100 * g.browse_error_rate)}% of browsing)
        fail continuously for {g.duration_s} s. Below that, errors stay until you roll back.
      </p>
      {g.last_triggered && <p className="mt-1 text-warn">Last triggered {new Date(g.last_triggered.at).toLocaleTimeString()}: {g.last_triggered.reason}</p>}
    </div>
  );
}

interface GraphInfo {
  nodes: string[];
  paused: boolean; pause_requested: boolean;
  checkpoint: { thread_id: string; checkpoint_id: string | null; next: string[]; checkpoints_saved: number;
                board_entries: number; trace: string[]; memory_recalled: boolean } | null;
}

/** The LangGraph run: its nodes, which have executed, the saved checkpoint, and Pause / Continue. */
function GraphProgress({ q }: { q: import("../api").Squad }) {
  const g = usePoll<GraphInfo>("/squad/graph", 2000);
  const { refreshAll } = useLive();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const info = g.data, cp = info?.checkpoint;
  const nodes = info?.nodes ?? q.graph_nodes ?? [];
  const done = new Set((q.active ? cp?.trace ?? q.graph_trace ?? [] : []).map(t => t.split(" ")[0]));
  const next = cp?.next?.[0];
  const isRunning = running(q) && !info?.paused;
  const mem = q.memory_recall;

  async function act(path: string, ok: string) {
    setBusy(true);
    try { await post(path); toast(ok, "ok"); } catch (e) { toast(`${(e as Error).message}`, "err"); }
    setBusy(false); g.refresh(); refreshAll();
  }

  return (
    <div className={cx("mt-4 rounded-lg border px-3 py-3", info?.paused ? "border-warn/50 bg-warn/5" : "border-line bg-surface-2")}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-[12.5px]">
          <span className="font-medium text-ink">LangGraph agent graph</span>
          {info?.paused ? <Badge tone="warn">Paused at checkpoint</Badge>
            : info?.pause_requested ? <Badge tone="warn">Pausing after current step…</Badge>
              : isRunning ? <Badge tone="accent">Running</Badge> : <Badge tone="idle">{q.active ? q.status.toLowerCase() : "idle"}</Badge>}
        </div>
        <div className="flex gap-2">
          <Button icon={<Pause className="h-4 w-4" />} busy={busy && !info?.paused}
            disabled={!isRunning || !!info?.pause_requested || !next}
            onClick={() => act("/squad/pause", "Pausing: the graph stops after the current step and keeps its memory.")}>Pause</Button>
          <Button variant="primary" icon={<Play className="h-4 w-4" />} busy={busy && !!info?.paused} disabled={!info?.paused}
            onClick={() => act("/squad/continue", "Continuing from the saved checkpoint with the same shared memory.")}>Continue</Button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1">
        {nodes.map((n, i) => (
          <span key={n} className="flex items-center gap-1">
            <span className={cx("rounded-md border px-2 py-0.5 font-mono text-[11px]",
              done.has(n) ? "border-ok/40 bg-ok/10 text-ok"
                : n === next && info?.paused ? "border-warn bg-warn/15 text-warn"
                  : n === next && isRunning ? "border-accent/60 bg-accent/10 text-accent" : "border-line text-ink-3")}>{n}</span>
            {i < nodes.length - 1 && <span className="text-ink-3">›</span>}
          </span>
        ))}
      </div>

      <div className="mt-3 grid grid-cols-4 gap-2 text-[11.5px] max-[1200px]:grid-cols-2">
        <Fact k="Next step" v={next ?? (q.active && done.size ? "finished" : "—")} />
        <Fact k="Checkpoints saved" v={cp ? String(cp.checkpoints_saved) : "—"} />
        <Fact k="Team board entries" v={cp ? String(cp.board_entries) : "—"} />
        <Fact k="Long-term memory" v={mem ? `${mem.runs} runs · ${mem.services_tested}/${mem.services_total} services` : "—"} />
      </div>
      <p className="mt-2 text-[11px] text-ink-3">
        Short-term memory = the graph State (team board, trace), saved by LangGraph's checkpointer after every step under
        thread <Mono>{cp?.thread_id ?? "—"}</Mono>. Pause stops at the next step boundary; Continue resumes the same thread from that
        checkpoint, so the agents keep everything they knew. Long-term memory = the Store of past runs (database).
        {info?.paused && q.status === "HOLDING" && " The injected errors stay in place while paused."}
      </p>
    </div>
  );
}

function Fact({ k, v }: { k: string; v: string }) {
  return (
    <div className="rounded-md border border-line bg-surface px-2 py-1.5">
      <div className="text-ink-3">{k}</div>
      <Mono className="text-ink">{v}</Mono>
    </div>
  );
}
const running = (q: import("../api").Squad) => ["PLANNING", "INJECTING", "HOLDING"].includes(q.status);