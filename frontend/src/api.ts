// Single client for the ResilienceAI backend (chaos/api.py). Types mirror its JSON responses.

export const API_URL: string = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function request<T>(method: "GET" | "POST", path: string, body?: unknown, timeoutMs = 15000): Promise<T> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res: Response;
  try {
    res = await fetch(API_URL + path, {
      method,
      cache: "no-store",
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    });
  } catch (e) {
    throw new ApiError(ctrl.signal.aborted ? "Request timed out" : "Backend unreachable", 0);
  } finally {
    clearTimeout(timer);
  }
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`;
    throw new ApiError(detail, res.status);
  }
  return data as T;
}

export const get = <T,>(path: string) => request<T>("GET", path);
export const post = <T,>(path: string, body?: unknown, timeoutMs?: number) =>
  request<T>("POST", path, body ?? {}, timeoutMs);

// ---------------------------------------------------------------- response types

export interface Baseline { n: number; error_rate: number; p50_ms: number; p95_ms: number }
export interface ActiveFault { service: string; fault: string; detail: string }
export interface Job { id: string | null; kind: string | null; status: string; error: string | null; started?: number; finished?: number }

export interface Status {
  ingestion: "RUNNING" | "STOPPED";
  experiment_status: string;
  active_faults: ActiveFault[];
  current_experiment: ({ run_id: string; target: string; failure_type: string; params?: { delay_ms?: number } } & Record<string, unknown>) | null;
  job: Job;
  kill_switch: boolean;
  baseline: Baseline | null;
  ai_backend: string;
  guardrail?: {
    error_rate: number; browse_error_rate: number; duration_s: number; armed: boolean; breaching_for_s: number;
    rolling_back: boolean; last_triggered: { at: string; reason: string } | null;
  };
}

export interface MetricRow {
  t: number; timestamp: string; n: number;
  error_rate: number | null; p50_ms: number | null; p95_ms: number | null; stalled: number;
  experiment: string | null; faults?: string[];
  browse_error_rate: number | null; cart_error_rate: number | null; checkout_error_rate: number | null;
}
export interface Health { healthy: boolean | null; error_rate?: number; p95_ms?: number; reason?: string }
export interface Metrics { baseline: Baseline | null; rows: MetricRow[]; health: Health }

export interface ServiceErrors {
  service: string; failed_requests: number; slow_requests: number; degraded_requests: number;
  types: string; last_seen: string; last_message: string; attributed_by: string; suspected_cause: string;
}
export interface ErrorEvent {
  t: number; timestamp: string; service: string; type: string; count: number; attributed_by: string;
  message: string; journeys: string[]; suspected_cause: string;
}
export interface Errors {
  window_s: number; total_requests: number; failed_requests: number; slow_requests: number; degraded_requests: number;
  affected_services: string[]; by_service: ServiceErrors[]; active: ErrorEvent[]; history: ErrorEvent[];
}

export interface EventRow { timestamp: string; category: string; event: string }
export interface ExperimentRow {
  id: string; target: string; fault: string; status: string; stop_reason: string | null; planned_by: string;
  error_rate_during: number | null; p95_ms_during: number | null; recovered: boolean | null; recovery_time_s: number | null;
}
export interface ServiceRow { service: string; ready: string; healthy: boolean; critical: boolean; calls: string; callers: string }

export interface Pick { failure_type: string; target: string; params: { delay_ms?: number }; hypothesis: string; reason: string }
export interface Agent {
  id: string; name: string; model: string; fault: string; status: string; thinking: string;
  pick: Pick | null; error: string | null; fallback: boolean; applied: boolean; shortlist?: string[];
  message?: string; recalled?: Record<string, { times_tested: number; worst?: { fault: string; score: number | null; severity: string | null } }>;
}
export interface Impact {
  requests_total: number; failed_requests: number; slow_requests: number; degraded_requests: number;
  failure_rate: number; slow_rate: number; pages_failed: string[]; pages_degraded: string[];
  journeys_failed: string[]; sample_messages: string[];
}
export interface Fix {
  likely_cause: string; immediate_fix: string; long_term_fix: string; prevention: string; confidence: string; model: string;
  error?: string;
}
export interface Squad {
  id: string | null; status: string; active: boolean; last_id: string | null;
  started: number | null; injected_at: number | null; note: string | null;
  analysis_status: string | null; analysis_error: string | null; observe_s: number; agents: Agent[];
  measured_requests?: number; min_requests?: number; observe_max_s?: number;
  graph_nodes?: string[]; graph_trace?: string[];
  memory_recall?: { runs: number; services_tested: number; services_total: number } | null;
  board?: { agent: string; model: string; role: string; target: string; reason: string; message: string }[];
  analyst: { preferred: string; fallback: string; model: string | null; note: string | null };
}

export type Severity = "Critical" | "High" | "Medium" | "Low";
export interface Finding {
  rank: number; severity: Severity; formula_score: number; target: string; failure_type: string;
  params: { delay_ms?: number }; agent: string; agent_model: string; ranked_by: string; model_severity?: string;
  justification: string; impact: Impact; suspected_cause: string; fix: Fix | null;
}
export interface Findings {
  squad_id: string | null; status: string; active: boolean; ingestion: string; analysis_status: string | null;
  analysis_error?: string | null; previous_squad_id?: string | null; analyst_model?: string;
  ranking_note?: string | null; resilience_score: number | null; score_basis?: string;
  weights?: Record<string, number>; findings: Finding[];
  last_rollback?: { at: string; recovery_verified: boolean; recovery_time_s: number | null; actions: string[] } | null;
}
export interface SquadRun {
  id: string; status: string; analyst_model: string | null;
  errors: { agent: string; model: string; target: string | null; failure_type: string | null }[];
  ranking: { rank: number; target: string; severity: string }[];
}
export interface RollbackResult {
  recovery_verified: boolean; recovery_time_s: number | null; actions: string[]; not_ready: string[];
  health: Health; note: string | null;
}

// ---------------------------------------------------------------- AWS extension (cloudresilience/, /cloud routes)

export interface CloudPublisher {
  running: boolean; namespace: string; environment: string; interval_s: number;
  windows_published: number; datums_published: number; last_publish: string | null; last_error: string | null;
}
export interface CloudActivity { at: string; kind: string; squad_id: string; folder?: string; notified?: boolean; error?: string }
export interface CloudStatus {
  connected: boolean; identity: { account: string; arn: string; user_id: string } | null; identity_error: string | null;
  config: {
    region: string | null; report_bucket: string | null; report_prefix: string; sns_topic_arn: string | null;
    metric_namespace: string; environment: string; publish_metrics: boolean; auto_report: boolean; ec2_tag_filter: string | null;
  };
  features: { inventory: boolean; metrics: boolean; reports: boolean; notifications: boolean };
  publisher: CloudPublisher; activity: CloudActivity[];
}
export interface Ec2Instance {
  instance_id: string; name: string | null; state: string; type: string | null; availability_zone: string | null;
  private_ip: string | null; public_ip: string | null; launch_time: string | null; platform: string;
}
export interface Ec2Inventory {
  instances: Ec2Instance[];
  summary: { total: number; by_state: Record<string, number>; by_type: Record<string, number>; by_az: Record<string, number> };
}
export interface MetricPoint { t: string; value: number }
export interface CloudSeries { series: Record<string, MetricPoint[]>; period_s: number; minutes: number }
export interface CloudAlarm { name: string; state: string; metric: string | null; threshold: number | null; comparison: string | null; reason: string | null; updated: string | null }
export interface CloudReport { key: string; markdown_key: string; squad_id: string; size: number; last_modified: string }
export interface CloudReportResult {
  squad_id: string; resilience_score: number | null; worst_severity: string | null; report_url: string;
  upload: { bucket: string; folder: string }; notification: { message_id: string } | null;
}

// ---------------------------------------------------------------- shared vocabulary

export const SERVICES = [
  "frontend", "cartservice", "checkoutservice", "paymentservice", "currencyservice", "productcatalogservice",
  "recommendationservice", "shippingservice", "emailservice", "adservice", "redis-cart",
];

export const FAULT_LABEL: Record<string, string> = {
  "service-down": "Outage",
  "pod-kill": "Crash loop",
  "network-delay": "Network latency",
};
