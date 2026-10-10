import { CartesianGrid, Legend, Line, LineChart, ReferenceArea, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { MetricRow } from "../api";

const AXIS = { stroke: "#64748b", fontSize: 11, fontFamily: "JetBrains Mono, monospace" };
const TOOLTIP = {
  contentStyle: { background: "#151d2f", border: "1px solid #2c3a54", borderRadius: 8, fontSize: 12 },
  labelStyle: { color: "#94a3b8", fontFamily: "JetBrains Mono, monospace" },
};

/** Contiguous runs of windows in which a fault was held, as [first, last] timestamps. */
export function faultWindows(rows: MetricRow[]): [string, string, string][] {
  const out: [string, string, string][] = [];
  let start: MetricRow | null = null, prev: MetricRow | null = null;
  const active = (r: MetricRow) => (r.faults?.length ?? 0) > 0 || !!r.experiment;
  for (const r of rows) {
    if (active(r) && !start) start = r;
    if (!active(r) && start && prev) { out.push([start.timestamp, prev.timestamp, label(start)]); start = null; }
    prev = r;
  }
  if (start && prev) out.push([start.timestamp, prev.timestamp, label(start)]);
  return out;
}
const label = (r: MetricRow) => (r.faults?.length ? `${r.faults.length} fault(s)` : "fault");

function Shading({ windows }: { windows: [string, string, string][] }) {
  return <>{windows.map(([a, b], i) => <ReferenceArea key={i} x1={a} x2={b} fill="#ef4444" fillOpacity={0.09} stroke="#ef4444" strokeOpacity={0.25} ifOverflow="extendDomain" />)}</>;
}

export function LatencyChart({ rows, baseline }: { rows: MetricRow[]; baseline: number | null }) {
  const data = rows.map(r => ({ t: r.timestamp, p95: r.n ? r.p95_ms : null }));
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
        <CartesianGrid stroke="#1f2a3e" vertical={false} />
        <XAxis dataKey="t" tick={AXIS} tickLine={false} axisLine={{ stroke: "#1f2a3e" }} minTickGap={40} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} unit=" ms" width={70} />
        <Tooltip {...TOOLTIP} formatter={(v: number) => [`${Math.round(v)} ms`, "p95"]} />
        <Shading windows={faultWindows(rows)} />
        {baseline != null && <ReferenceLine y={baseline} stroke="#94a3b8" strokeDasharray="5 4" label={{ value: "baseline", fill: "#94a3b8", fontSize: 10, position: "insideTopRight" }} />}
        <Line type="monotone" dataKey="p95" stroke="#2dd4bf" strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}

export function ErrorRateChart({ rows, baseline }: { rows: MetricRow[]; baseline: number | null }) {
  const v = (x: number | null) => (x == null ? null : +(100 * x).toFixed(2));
  const data = rows.map(r => ({ t: r.timestamp, all: v(r.n ? r.error_rate : null), browse: v(r.browse_error_rate), cart: v(r.cart_error_rate), checkout: v(r.checkout_error_rate) }));
  return (
    <ResponsiveContainer width="100%" height={240}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: -8, bottom: 0 }}>
        <CartesianGrid stroke="#1f2a3e" vertical={false} />
        <XAxis dataKey="t" tick={AXIS} tickLine={false} axisLine={{ stroke: "#1f2a3e" }} minTickGap={40} />
        <YAxis tick={AXIS} tickLine={false} axisLine={false} unit="%" domain={[0, 100]} width={56} />
        <Tooltip {...TOOLTIP} formatter={(val: number, name: string) => [`${val}%`, name]} />
        <Legend wrapperStyle={{ fontSize: 11, color: "#94a3b8" }} iconType="plainline" />
        <Shading windows={faultWindows(rows)} />
        {baseline != null && <ReferenceLine y={100 * baseline} stroke="#94a3b8" strokeDasharray="5 4" />}
        <Line name="all requests" type="monotone" dataKey="all" stroke="#2dd4bf" strokeWidth={2} dot={false} isAnimationActive={false} />
        <Line name="browse" type="monotone" dataKey="browse" stroke="#64748b" strokeWidth={1.3} dot={false} isAnimationActive={false} />
        <Line name="cart" type="monotone" dataKey="cart" stroke="#a78bfa" strokeWidth={1.3} dot={false} isAnimationActive={false} />
        <Line name="checkout" type="monotone" dataKey="checkout" stroke="#f472b6" strokeWidth={1.3} dot={false} isAnimationActive={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
