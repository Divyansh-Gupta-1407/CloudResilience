import { memo, useCallback, useEffect, useMemo, useRef } from "react";
import { LocateFixed } from "lucide-react";
import { Background, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps, type ReactFlowInstance } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { FAULT_LABEL, type ActiveFault, type Agent, type ServiceErrors, type ServiceRow } from "../api";
import { cx } from "../ui";

// Fixed left-to-right layout, kept wide and flat so it stays readable in a short panel:
// traffic -> entry point -> page features + orchestrator -> core services -> datastore / leaf services.
const POS: Record<string, [number, number]> = {
  loadgenerator: [0, 150],
  frontend: [240, 150],
  adservice: [510, 20],
  recommendationservice: [510, 140],
  checkoutservice: [510, 260],
  productcatalogservice: [800, 0],
  currencyservice: [800, 95],
  cartservice: [800, 190],
  shippingservice: [800, 285],
  emailservice: [1090, 0],
  paymentservice: [1090, 95],
  "redis-cart": [1090, 190],
};

export type Health = "fault" | "chosen" | "down" | "affected" | "healthy" | "external";
export const HEALTH_STYLE: Record<Health, { box: string; dot: string; label: string }> = {
  fault: { box: "border-bad bg-bad/15", dot: "bg-bad", label: "Fault injected" },
  chosen: { box: "border-warn border-dashed bg-warn/5", dot: "bg-warn", label: "Chosen by agent" },
  down: { box: "border-bad/70 bg-bad/10", dot: "bg-bad", label: "Not ready" },
  affected: { box: "border-warn/70 bg-warn/10", dot: "bg-warn", label: "Affected" },
  healthy: { box: "border-line-strong bg-surface-2", dot: "bg-ok", label: "Healthy" },
  external: { box: "border-line bg-surface border-dashed", dot: "bg-idle", label: "Traffic source" },
};

interface NodeData extends Record<string, unknown> {
  name: string; health: Health; ready: string; tag?: string; counts?: string;
}
type SvcNode = Node<NodeData, "svc">;

const ServiceNode = memo(function ServiceNode({ data }: NodeProps<SvcNode>) {
  const s = HEALTH_STYLE[data.health];
  return (
    <div className={cx("w-[212px] rounded-lg border px-3 py-2", s.box)}>
      <Handle type="target" position={Position.Left} />
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-[12.5px] text-ink">{data.name}</span>
        <span className="font-mono text-[11px] text-ink-3">{data.ready}</span>
      </div>
      <div className="mt-1 flex items-center gap-1.5 text-[11px] text-ink-2">
        <span className={cx("h-1.5 w-1.5 rounded-full", s.dot)} />{s.label}
      </div>
      {data.tag && <div className="mt-1 truncate text-[11px] font-medium text-bad">{data.tag}</div>}
      {data.counts && <div className="mt-0.5 truncate font-mono text-[10.5px] text-warn">{data.counts}</div>}
      <Handle type="source" position={Position.Right} />
    </div>
  );
});

const nodeTypes = { svc: ServiceNode };
const FIT = { padding: 0.04 };

/** Service dependency map: every deployment, who calls whom, and its live state. */
export function DependencyGraph({ services, faults, errors, agents, height = 420 }: {
  services: ServiceRow[]; faults: ActiveFault[]; errors: ServiceErrors[]; agents: Agent[]; height?: number;
}) {
  const flow = useRef<ReactFlowInstance<SvcNode> | null>(null);
  const box = useRef<HTMLDivElement>(null);

  // The panel re-renders on every 1-2 s poll. Rebuild the graph only when what it displays actually
  // changes, so nodes are not re-measured (flicker) and animated edges do not restart each poll.
  const view = useMemo(() => {
    const faultBy = new Map(faults.map(f => [f.service, f]));
    const errBy = new Map(errors.map(e => [e.service, e]));
    const agentBy = new Map(agents.filter(a => a.pick).map(a => [a.pick!.target, a]));
    const nodes: SvcNode[] = services.map((s, i) => {
      const f = faultBy.get(s.service), e = errBy.get(s.service), a = agentBy.get(s.service);
      const health: Health = s.service === "loadgenerator" ? "external"
        : f ? "fault" : a && !a.applied ? "chosen" : !s.healthy ? "down" : e ? "affected" : "healthy";
      const what = a ? `${FAULT_LABEL[a.pick!.failure_type] ?? a.pick!.failure_type}${a.pick!.params.delay_ms ? ` +${a.pick!.params.delay_ms} ms` : ""}` : null;
      const tag = a ? `Agent ${a.id}: ${what}` : f ? `${f.fault} (${f.detail})` : undefined;
      const counts = e ? [e.failed_requests && `${e.failed_requests} failed`, e.slow_requests && `${e.slow_requests} slow`,
        e.degraded_requests && `${e.degraded_requests} degraded`].filter(Boolean).join(" · ") : undefined;
      const [x, y] = POS[s.service] ?? [1200, i * 85];
      return { id: s.service, type: "svc", position: { x, y }, draggable: false,
        data: { name: s.service, health, ready: s.ready, tag, counts: counts || undefined } };
    });
    const names = new Set(services.map(s => s.service));
    const edges: Edge[] = services.flatMap(s => s.calls.split(",").map(c => c.trim()).filter(c => names.has(c)).map(c => {
      const hot = faultBy.has(c);
      return { id: `${s.service}->${c}`, source: s.service, target: c, animated: hot,
        style: { stroke: hot ? "#ef4444" : "#334155", strokeWidth: hot ? 2 : 1.2 } };
    }));
    // The load generator reaches the shop through the frontend's HTTP port, not an *_ADDR variable.
    if (names.has("loadgenerator") && names.has("frontend") && !edges.some(e => e.source === "loadgenerator"))
      edges.push({ id: "loadgenerator->frontend", source: "loadgenerator", target: "frontend", style: { stroke: "#334155", strokeDasharray: "4 4" } });
    return { nodes, edges };
  }, [services, faults, errors, agents]);
  const signature = JSON.stringify([view.nodes.map(n => n.data), view.edges.map(e => [e.id, e.animated])]);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const { nodes, edges } = useMemo(() => view, [signature]);

  // Fit after layout has settled (next frame), on first load, when services appear, and on resize.
  const fit = useCallback(() => {
    requestAnimationFrame(() => flow.current?.fitView(FIT));
  }, []);
  useEffect(() => {
    if (!box.current) return;
    let t: ReturnType<typeof setTimeout>;
    const ro = new ResizeObserver(() => { clearTimeout(t); t = setTimeout(fit, 120); });
    ro.observe(box.current);
    return () => { clearTimeout(t); ro.disconnect(); };
  }, [fit]);
  useEffect(() => { fit(); }, [nodes.length, fit]);

  return (
    <div ref={box} className="relative w-full" style={{ height }}>
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={FIT}
        onInit={i => { flow.current = i; fit(); }} nodesDraggable={false} nodesConnectable={false} elementsSelectable={false}
        panOnDrag zoomOnScroll={false} zoomOnDoubleClick={false} zoomOnPinch preventScrolling={false} minZoom={0.3} maxZoom={1.6}
        proOptions={{ hideAttribution: true }} colorMode="dark">
        <Background color="#1a2335" gap={16} />
      </ReactFlow>
      <button onClick={fit} title="Re-centre the map"
        className="absolute right-2 top-2 z-10 flex h-7 items-center gap-1.5 rounded-md border border-line-strong bg-surface-2/90 px-2 text-[11px] text-ink-2 hover:text-ink">
        <LocateFixed className="h-3.5 w-3.5" />Re-centre
      </button>
    </div>
  );
}
