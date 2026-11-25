"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type Status = "operational" | "degraded" | "down" | "unknown";
export type Window = "24h" | "7d" | "30d";

export type StorageInfo = { backend: "sqlite" | "mongodb"; label: string; persistent: boolean; atlas: boolean; location: string };
export type Meta = { generated_at: string; storage: StorageInfo; version: string };

export type UptimeStats = {
  window: Window;
  window_start: string;
  window_end: string;
  window_seconds: number;
  observed_seconds: number;
  operational_seconds: number;
  degraded_seconds: number;
  down_seconds: number;
  unknown_seconds: number;
  coverage_pct: number;
  sufficient: boolean;
  uptime_pct: number | null;
  availability_pct: number | null;
  check_count: number;
  failed_check_count: number;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
  avg_functional_latency_ms: number | null;
  monitoring_started_at: string | null;
};

export type Bar = {
  start: string;
  end: string;
  status: Status;
  uptime_pct: number | null;
  coverage_pct: number;
  operational_seconds: number;
  degraded_seconds: number;
  down_seconds: number;
  checks: number;
  failed_checks: number;
};

export type Current = {
  status: Status;
  summary: string;
  since: string | null;
  last_checked_at: string | null;
  last_success_at: string | null;
  stale: boolean;
  failure_type: string | null;
  open_incident_id: string | null;
};

export type ServiceOverview = { id: string; name: string; app: string; description: string; current: Current; uptime: UptimeStats; bars: Bar[] | null; latest_check: Check | null };

export type IncidentEvent = { at: string; type: string; status: Status | null; message: string; check_id: string | null };

export type StepState = "waiting" | "running" | "done" | "failed" | "needs_review" | "skipped";
export type WalkStep = {
  key: string;
  label: string;
  state: StepState;
  started_at: string | null;
  ended_at: string | null;
  duration_seconds: number | null;
  summary: string;
  details: Record<string, unknown>;
};
export type Walkthrough = {
  incident_id: string;
  title: string;
  state: "open" | "resolved";
  steps: WalkStep[];
  health: { state: "on_track" | "slow" | "attention" | "done"; reason: string };
};

export type Incident = {
  id: string;
  service_id: string;
  service_name: string;
  app: string;
  state: "open" | "resolved";
  severity: "degraded" | "down";
  peak_severity: "degraded" | "down";
  failure_type: string | null;
  investigation_route: "service_health" | "investigate";
  title: string;
  summary: string;
  started_at: string;
  last_failure_at: string;
  resolved_at: string | null;
  duration_seconds: number;
  failed_check_count: number;
  first_check_id: string;
  last_check_id: string;
  recovery_check_id: string | null;
  evidence: Record<string, unknown> & { route_reason?: string };
  events: IncidentEvent[];
  links: { kind: string; url: string; label: string; source: string }[];
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  related_ci: { id: string; status: string; stage: string; run_url: string | null; pr_url: string | null; commit_sha: string | null; created_at: string }[];
  service_health: Status;
  repo_agent?: { enabled: boolean; mode: "diagnose" | "report_only" | "draft_pr" } | null;
};

export type Worker = {
  owner: string | null;
  lease_expires_at: string | null;
  started_at: string | null;
  heartbeat_at: string | null;
  last_cycle_started_at: string | null;
  last_cycle_finished_at: string | null;
  last_cycle_duration_ms: number | null;
  cycles_completed: number;
  skipped_slots: number;
  duplicate_slots: number;
  interval_seconds: number;
};

export type Monitoring = {
  status: "running" | "stale" | "not_started";
  message: string;
  interval_seconds: number;
  worker: Worker | null;
  lease_held: boolean;
  next_cycle_expected_at: string | null;
  total_checks: number;
  storage: StorageInfo;
};

export type AgentActivity = {
  incident_id: string;
  service_name: string;
  state: "unavailable" | "waiting" | "working" | "awaiting_review" | "reported" | "needs_attention" | "finished";
  stage: string | null;
  started_at: string | null;
  updated_at: string | null;
  latest_action: { stage: string; outcome: string; at: string } | null;
  pr_url: string | null;
  fix_confirmation: "confirmed" | "disputed" | null;
};

export type DependencyCheck = {
  id: "github" | "mongodb" | "openai";
  name: string;
  status: "operational" | "degraded" | "down" | "not_configured";
  summary: string;
  action: string | null;
  checked_at: string;
  latency_ms: number | null;
};

export type LearningStrategy = { app: string; action: string; tries: number; successes: number; success_rate: number; real_tries: number };
export type LearningCurvePoint = { epoch: number; incidents: string; first_try_rate: number; avg_attempts: number };
export type Learning = {
  available: boolean;
  totals: { experiences?: number; real_experiences?: number; mock_incidents?: number; memories?: number };
  strategies: LearningStrategy[];
  training: {
    at: string;
    embedding_model: string | null;
    accuracy_before: number;
    accuracy_after: number;
    per_bug: Record<string, number>;
    eval_incidents: number | null;
    curve: LearningCurvePoint[];
    mock_incidents: number | null;
    epochs: number | null;
  } | null;
  decisions: {
    incident_id: string;
    app: string | null;
    source: string;
    status: string | null;
    created_at: string | null;
    action: string | null;
    expected_success: number | null;
    hinted: boolean;
    pr_url: string | null;
  }[];
  vector_indexes: { collection: string; name: string | null; status: string | null; queryable: boolean }[];
};

export type Summary = {
  overall: { status: Status; label: string; affected_services: string[] };
  window: Window;
  services: ServiceOverview[];
  active_incidents: Incident[];
  recent_incidents: Incident[];
  monitoring: Monitoring;
  agent_activity: AgentActivity[];
  repositories: RepoOverview[];
  setup: { workspace_id: string; github_connected: boolean; betterstack_configured: boolean; notifications_configured: boolean };
};

export type AgentMode = "diagnose" | "report_only" | "draft_pr";
export type RepoOverview = {
  service_id: string; service_name: string; repo: string; base_branch: string; deployed_url: string; workflow: string | null;
  betterstack_monitor_id: string | null; agent: { enabled: boolean; mode: AgentMode; app: string | null };
  health: Status; open_incident_id: string | null;
};

export type Probe = {
  kind: "frontend" | "availability" | "functional";
  url: string;
  ok: boolean;
  http_status: number | null;
  latency_ms: number | null;
  error_kind: string | null;
  error: string | null;
  details: {
    status?: string;
    demo_outage?: boolean;
    summary?: string;
    failed_checks?: string[];
    failures?: { check: string; component?: string; reason: string; source?: string | null; expected?: unknown; actual?: unknown }[];
  } & Record<string, unknown>;
};

export type Check = {
  id: string;
  service_id: string;
  checked_at: string;
  scheduled: boolean;
  status: Status;
  failure_type: string | null;
  summary: string;
  latency_ms: number | null;
  functional_latency_ms: number | null;
  frontend: Probe | null;
  availability: Probe | null;
  functional: Probe | null;
  interval_seconds: number;
  worker_id: string;
};

export type ServiceConfig = {
  id: string;
  name: string;
  app: string;
  description: string;
  frontend_url: string;
  backend_url: string;
  availability_path: string;
  functional_path: string;
  functional_method: string;
  interval_seconds: number;
  timeout_seconds: number;
  functional_timeout_seconds: number;
  failure_threshold: number;
  recovery_threshold: number;
  enabled: boolean;
};

export type ServiceDetail = { service: ServiceConfig; current: Current; uptime: Record<Window, UptimeStats>; incidents: Incident[]; incident_ids: string[] };

export function isDemoIncident(incident: Incident): boolean {
  const latest = incident.evidence.latest_failure as { probes?: { functional?: { details?: { demo_outage?: boolean } } } } | undefined;
  return latest?.probes?.functional?.details?.demo_outage === true;
}
export type LatencyPoint = { start: string; availability_avg_ms: number | null; availability_max_ms: number | null; functional_avg_ms: number | null; functional_max_ms: number | null; samples: number };

type Envelope<T> = { data: T; meta: Meta };

/** Where a request failed, so the page can say what to fix. */
export type ErrorKind = "dashboard_offline" | "api_unreachable" | "not_found" | "rejected" | "api_error" | "bad_response";

export class ApiError extends Error {
  constructor(public status: number, message: string, public kind: ErrorKind) {
    super(message);
  }
}

/** fetch() itself threw: the browser could not reach the dashboard server that proxies the API. */
export function networkError(request: string): ApiError {
  return new ApiError(0, `The dashboard server did not respond to ${request}. Check that the dashboard is still running (port 3300).`, "dashboard_offline");
}

/** A non-2xx response. The uptime API always answers with {"error": {code, message}}; anything else came from the proxy. */
export async function responseError(res: Response, request: string): Promise<ApiError> {
  const body = await res.json().catch(() => null);
  const err = body?.error as { code?: string; message?: string } | undefined;
  if (typeof err?.message === "string") {
    const kind = res.status === 404 ? "not_found" : res.status < 500 ? "rejected" : "api_error";
    return new ApiError(res.status, `${err.message} (${err.code ?? "ERROR"}, HTTP ${res.status} on ${request})`, kind);
  }
  if (res.status === 404) {
    return new ApiError(404, `${request} does not exist on the running uptime API. It is probably older than this dashboard: restart it.`, "not_found");
  }
  if (res.status >= 500) {
    return new ApiError(res.status, `The uptime API is not answering: the dashboard proxy got HTTP ${res.status} for ${request}. Start the API or check OPSRELAY_UPTIME_API_URL.`, "api_unreachable");
  }
  return new ApiError(res.status, `The uptime API rejected ${request} with HTTP ${res.status}.`, "rejected");
}

async function getJson<T>(path: string): Promise<Envelope<T>> {
  const request = `GET ${path.split("?")[0]}`;
  let res: Response;
  try {
    res = await fetch(path, { cache: "no-store" });
  } catch {
    throw networkError(request);
  }
  // Signed out (401) or no workspace yet (409): the sign-in page handles both.
  if ((res.status === 401 || res.status === 409) && !path.startsWith("/api/v1/auth/")) window.location.assign("/sign-in");
  if (!res.ok) throw await responseError(res, request);
  const body = await res.json().catch(() => null);
  if (!body || !("data" in body)) throw new ApiError(res.status, `The uptime API answered ${request} with something other than JSON data.`, "bad_response");
  return body as Envelope<T>;
}

export type Account = { user: { id: string; email: string }; workspaces: { id: string; name: string }[]; workspace_id: string | null };

/** Which workspace the API reads; the server checks the signed-in account owns it. */
export function selectWorkspace(id: string) {
  document.cookie = `opsrelay_workspace=${encodeURIComponent(id)}; path=/; max-age=31536000; samesite=lax`;
}

/** A mutation; resolves to the envelope's data or throws an ApiError with the API's message. */
export async function send<T>(method: "GET" | "POST" | "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
  const request = `${method} ${path}`;
  let res: Response;
  try {
    res = await fetch(path, { method, headers: body === undefined ? undefined : { "Content-Type": "application/json" },
                              body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw networkError(request);
  }
  if (!res.ok) throw await responseError(res, request);
  return (await res.json()).data as T;
}

export const POLL_MS = 10_000;
/** The status overview refreshes every 30s; sites are checked every 5 minutes (or on "Check now"). */
export const STATUS_POLL_MS = 30_000;

/** Poll an endpoint without page reloads. Keeps the last good data while a refresh is in flight or failing. */
export function usePoll<T>(path: string | null, intervalMs = POLL_MS) {
  const [data, setData] = useState<T | null>(null);
  const [meta, setMeta] = useState<Meta | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const pathRef = useRef(path);
  pathRef.current = path;

  const load = useCallback(async () => {
    const current = pathRef.current;
    if (!current) return;
    try {
      const env = await getJson<T>(current);
      if (pathRef.current !== current) return;
      setData(env.data);
      setMeta(env.meta);
      setError(null);
      setUpdatedAt(Date.now());
    } catch (e) {
      if (pathRef.current === current) setError(e as ApiError);
    } finally {
      if (pathRef.current === current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!path) return;
    setLoading(true);
    load();
    const t = window.setInterval(load, intervalMs);
    const onVisible = () => document.visibilityState === "visible" && load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(t);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [path, intervalMs, load]);

  return { data, meta, error, updatedAt, loading, reload: load };
}
