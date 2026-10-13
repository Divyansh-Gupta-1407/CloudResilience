import { useState } from "react";
import { Bell, Cloud as CloudIcon, CloudUpload, Download, FileText, Play, Radio, Server, Square } from "lucide-react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  get, post, type CloudAlarm, type CloudReport, type CloudReportResult, type CloudSeries, type CloudStatus, type Ec2Inventory,
} from "../api";
import { DataTable } from "../components/DataTable";
import { usePoll } from "../hooks";
import { Badge, Button, Card, DataState, Mono, cx, useToast, type Tone } from "../ui";

const STATE_TONE: Record<string, Tone> = { running: "ok", stopped: "idle", pending: "warn", stopping: "warn", terminated: "bad" };
const ALARM_TONE: Record<string, Tone> = { OK: "ok", ALARM: "bad", INSUFFICIENT_DATA: "idle" };
const AXIS = { stroke: "#64748b", fontSize: 11, fontFamily: "JetBrains Mono, monospace" };
const time = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

/** AWS side of the platform: identity, CloudWatch telemetry, alarms, EC2 inventory and S3 incident reports. */
export function Cloud() {
  const st = usePoll<CloudStatus>("/cloud/status", 10000);
  const s = st.data;
  const connected = !!s?.connected;
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-[1fr_1fr] gap-4 max-[1200px]:grid-cols-1">
        <Connection st={st} />
        <Publisher status={s} refresh={st.refresh} />
      </div>
      <TelemetryFromCloudWatch enabled={connected} />
      <div className="grid grid-cols-[1fr_1fr] gap-4 max-[1200px]:grid-cols-1">
        <Alarms enabled={connected} />
        <Reports status={s} refresh={st.refresh} />
      </div>
      <Inventory enabled={connected} />
    </div>
  );
}

function Connection({ st }: { st: ReturnType<typeof usePoll<CloudStatus>> }) {
  const s = st.data;
  return (
    <Card title="AWS connection" icon={<CloudIcon className="h-4 w-4" />}
      actions={s && <Badge tone={s.connected ? "ok" : "bad"}>{s.connected ? "Connected" : "Not connected"}</Badge>}>
      <DataState loading={st.loading} error={st.error} hasData={!!s}>
        {s && (
          <dl className="grid grid-cols-[150px_1fr] gap-y-2 text-[13px]">
            <dt className="text-ink-3">Account</dt><dd><Mono>{s.identity?.account ?? "—"}</Mono></dd>
            <dt className="text-ink-3">Acting as</dt><dd><Mono className="break-all text-[11.5px]">{s.identity?.arn ?? s.identity_error ?? "—"}</Mono></dd>
            <dt className="text-ink-3">Region</dt><dd><Mono>{s.config.region ?? "not set"}</Mono></dd>
            <dt className="text-ink-3">Report bucket</dt><dd><Mono>{s.config.report_bucket ? `s3://${s.config.report_bucket}/${s.config.report_prefix}` : "not configured (CR_REPORT_BUCKET)"}</Mono></dd>
            <dt className="text-ink-3">SNS topic</dt><dd><Mono className="break-all text-[11.5px]">{s.config.sns_topic_arn ?? "not configured (CR_SNS_TOPIC_ARN)"}</Mono></dd>
            <dt className="text-ink-3">Auto report</dt><dd><Badge tone={s.config.auto_report ? "ok" : "idle"}>{s.config.auto_report ? "After every squad rollback" : "Off (CR_AUTO_REPORT)"}</Badge></dd>
          </dl>
        )}
      </DataState>
    </Card>
  );
}

function Publisher({ status, refresh }: { status: CloudStatus | null; refresh: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const p = status?.publisher;
  async function toggle() {
    setBusy(true);
    try {
      await post(`/cloud/telemetry/publish/${p?.running ? "stop" : "start"}`);
      toast(p?.running ? "Stopped streaming telemetry to CloudWatch." : "Streaming telemetry windows to CloudWatch.", "ok");
    } catch (e) { toast(`Publisher: ${(e as Error).message}`, "err"); }
    setBusy(false); refresh();
  }
  return (
    <Card title="CloudWatch telemetry stream" icon={<Radio className="h-4 w-4" />}
      actions={<Button variant={p?.running ? "secondary" : "primary"} busy={busy} disabled={!status?.connected}
        icon={p?.running ? <Square className="h-4 w-4" /> : <Play className="h-4 w-4" />} onClick={toggle}>
        {p?.running ? "Stop" : "Start"} publishing</Button>}>
      {!p ? <span className="text-sm text-ink-3">No data</span> : (
        <dl className="grid grid-cols-[150px_1fr] gap-y-2 text-[13px]">
          <dt className="text-ink-3">State</dt><dd><Badge tone={p.running ? "ok" : "idle"}>{p.running ? `Running (every ${p.interval_s} s)` : "Stopped"}</Badge></dd>
          <dt className="text-ink-3">Namespace</dt><dd><Mono>{p.namespace}</Mono> · Environment=<Mono>{p.environment}</Mono></dd>
          <dt className="text-ink-3">Windows sent</dt><dd><Mono>{p.windows_published}</Mono> <span className="text-ink-3">({p.datums_published} datapoints)</span></dd>
          <dt className="text-ink-3">Last publish</dt><dd><Mono>{p.last_publish ? new Date(p.last_publish).toLocaleTimeString() : "—"}</Mono></dd>
          {p.last_error && <><dt className="text-ink-3">Last error</dt><dd className="text-bad text-xs">{p.last_error}</dd></>}
        </dl>
      )}
    </Card>
  );
}

function TelemetryFromCloudWatch({ enabled }: { enabled: boolean }) {
  const t = usePoll<CloudSeries>(enabled ? "/cloud/telemetry?minutes=60" : null, 30000);
  const series = t.data?.series ?? {};
  const byTime = new Map<string, Record<string, number | string>>();
  for (const [name, pts] of Object.entries(series))
    for (const p of pts) byTime.set(p.t, { ...(byTime.get(p.t) ?? { t: time(p.t) }), [name]: p.value });
  const data = [...byTime.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([, v]) => v);
  return (
    <Card title="Shopper experience, read back from CloudWatch (last 60 min, 1-min periods)" icon={<CloudIcon className="h-4 w-4" />}>
      <DataState loading={enabled && t.loading} error={t.error} hasData={!!t.data} empty={!enabled || data.length === 0}
        emptyText={enabled ? "No datapoints yet: start publishing and run telemetry" : "Not connected to AWS"}>
        <div className="grid grid-cols-2 gap-4 max-[1200px]:grid-cols-1">
          <MiniChart data={data} lines={[["ErrorRate", "#ef4444", "Error rate %"]]} unit="%" />
          <MiniChart data={data} lines={[["LatencyP95", "#2dd4bf", "p95 ms"], ["ActiveFaults", "#f59e0b", "faults held"]]} />
        </div>
      </DataState>
    </Card>
  );
}

function MiniChart({ data, lines, unit }: { data: Record<string, number | string>[]; lines: [string, string, string][]; unit?: string }) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
        <CartesianGrid stroke="#1f2a3e" vertical={false} />
        <XAxis dataKey="t" tick={AXIS} tickLine={false} axisLine={{ stroke: "#1f2a3e" }} minTickGap={40} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} unit={unit} width={60} />
        <Tooltip contentStyle={{ background: "#151d2f", border: "1px solid #2c3a54", borderRadius: 8, fontSize: 12 }} />
        {lines.map(([key, color, name]) => <Line key={key} dataKey={key} name={name} stroke={color} strokeWidth={2} dot={false} isAnimationActive={false} />)}
      </LineChart>
    </ResponsiveContainer>
  );
}

function Alarms({ enabled }: { enabled: boolean }) {
  const a = usePoll<CloudAlarm[]>(enabled ? "/cloud/alarms" : null, 30000);
  return (
    <Card title="CloudWatch alarms" icon={<Bell className="h-4 w-4" />}>
      <DataState loading={enabled && a.loading} error={a.error} hasData={!!a.data} empty={!enabled || !a.data?.length}
        emptyText={enabled ? "No CloudResilience alarms: deploy cloudresilience/infra/cloudformation.yaml" : "Not connected to AWS"}>
        <ul className="flex flex-col gap-2">
          {a.data?.map(x => (
            <li key={x.name} className="flex items-start justify-between gap-3 rounded-lg border border-line bg-surface-2 px-3 py-2">
              <div className="min-w-0">
                <Mono className="text-[12px]">{x.name}</Mono>
                <div className="truncate text-xs text-ink-3" title={x.reason ?? ""}>{x.metric} {x.comparison} {x.threshold}</div>
              </div>
              <Badge tone={ALARM_TONE[x.state] ?? "idle"}>{x.state}</Badge>
            </li>
          ))}
        </ul>
      </DataState>
    </Card>
  );
}

function Reports({ status, refresh }: { status: CloudStatus | null; refresh: () => void }) {
  const enabled = !!status?.connected && !!status.features.reports;
  const r = usePoll<CloudReport[]>(enabled ? "/cloud/reports?limit=20" : null, 30000);
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  async function upload() {
    setBusy(true);
    try {
      const res = await post<CloudReportResult>("/cloud/reports", { notify: true }, 60000);
      toast(`Report for squad ${res.squad_id} uploaded to S3${res.notification ? " and sent to SNS" : ""}.`, "ok");
    } catch (e) { toast(`Report upload failed: ${(e as Error).message}`, "err"); }
    setBusy(false); r.refresh(); refresh();
  }
  async function open(key: string) {
    try { window.open((await get<{ url: string }>(`/cloud/reports/url?key=${encodeURIComponent(key)}`)).url, "_blank", "noopener"); }
    catch (e) { toast(`Download link failed: ${(e as Error).message}`, "err"); }
  }
  return (
    <Card title="Incident reports in S3" icon={<FileText className="h-4 w-4" />}
      actions={<Button variant="primary" icon={<CloudUpload className="h-4 w-4" />} busy={busy} disabled={!enabled} onClick={upload}>Upload latest run</Button>}>
      <DataState loading={enabled && r.loading} error={r.error} hasData={!!r.data} empty={!enabled || !r.data?.length}
        emptyText={!status?.connected ? "Not connected to AWS" : !status.features.reports ? "Set CR_REPORT_BUCKET to store reports" : "No reports yet"}>
        <ul className="flex flex-col gap-2">
          {r.data?.map(x => (
            <li key={x.key} className="flex items-center justify-between gap-3 rounded-lg border border-line bg-surface-2 px-3 py-2">
              <div className="min-w-0">
                <Mono className="text-[12px]">{x.squad_id}</Mono>
                <div className="text-xs text-ink-3">{new Date(x.last_modified).toLocaleString()}</div>
              </div>
              <div className="flex gap-1.5">
                <Button variant="ghost" icon={<Download className="h-3.5 w-3.5" />} onClick={() => open(x.markdown_key)}>.md</Button>
                <Button variant="ghost" icon={<Download className="h-3.5 w-3.5" />} onClick={() => open(x.key)}>.json</Button>
              </div>
            </li>
          ))}
        </ul>
      </DataState>
    </Card>
  );
}

function Inventory({ enabled }: { enabled: boolean }) {
  const inv = usePoll<Ec2Inventory>(enabled ? "/cloud/inventory" : null, 60000);
  const sum = inv.data?.summary;
  return (
    <Card title="EC2 inventory" icon={<Server className="h-4 w-4" />}
      actions={sum && <span className="flex gap-1.5">{Object.entries(sum.by_state).map(([k, n]) =>
        <Badge key={k} tone={STATE_TONE[k] ?? "idle"}>{n} {k}</Badge>)}</span>}>
      <DataState loading={enabled && inv.loading} error={inv.error} hasData={!!inv.data} empty={!enabled}
        emptyText="Not connected to AWS">
        <DataTable rows={inv.data?.instances ?? []} filterable empty="No EC2 instances in this region" maxHeight={360} columns={[
          { key: "instance_id", header: "Instance", render: x => <Mono>{x.instance_id}</Mono> },
          { key: "name", header: "Name" },
          { key: "state", header: "State", render: x => <Badge tone={STATE_TONE[x.state] ?? "idle"}>{x.state}</Badge> },
          { key: "type", header: "Type", render: x => <Mono>{x.type ?? "—"}</Mono> },
          { key: "availability_zone", header: "AZ", render: x => <Mono>{x.availability_zone ?? "—"}</Mono> },
          { key: "private_ip", header: "Private IP", render: x => <Mono className={cx(!x.private_ip && "text-ink-3")}>{x.private_ip ?? "—"}</Mono> },
          { key: "launch_time", header: "Launched", render: x => x.launch_time ? new Date(x.launch_time).toLocaleString() : "—" },
        ]} />
      </DataState>
    </Card>
  );
}
