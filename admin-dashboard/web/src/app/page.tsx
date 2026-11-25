"use client";

import Link from "next/link";
import { useState } from "react";
import { StatusLegend, UptimeBars } from "@/components/charts";
import { STATES } from "@/components/agent-activity";
import { IncidentList } from "@/components/incidents";
import { Onboarding } from "@/components/onboarding";
import { ErrorPanel, Shell, Skeleton, StatusText, WindowTabs } from "@/components/ui";
import { type AgentActivity, type RepoOverview, STATUS_POLL_MS, type ServiceOverview, type Summary, type Window, usePoll } from "@/lib/api";
import { ago, clock, ms, pct, useNow } from "@/lib/format";
import { checkFacts } from "@/lib/phase";

const COLS = "lg:grid lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1.3fr)_7.5rem_minmax(0,1.6fr)] lg:gap-6";

function ServiceRow({ s, window }: { s: ServiceOverview; window: Window }) {
  const now = useNow();
  const facts = checkFacts(s.latest_check);
  return (
    <li className={`border-t border-line px-4 py-4 first:border-t-0 sm:px-5 ${COLS} lg:items-center`}>
      <div className="min-w-0">
        <Link href={`/services/${s.id}`} className="font-medium text-ink hover:underline">{s.name}</Link>
        <div className="mt-0.5 text-sm">
          <StatusText status={s.current.status} className={s.current.status === "operational" ? "text-muted" : "font-medium text-ink"} />
          {s.current.open_incident_id && (
            <Link href={`/incidents/${encodeURIComponent(s.current.open_incident_id)}`} className="ml-2 text-xs text-accent hover:underline">
              Open incident
            </Link>
          )}
        </div>
      </div>
      <ul className="mt-2 space-y-0.5 text-xs text-muted lg:mt-0" aria-label={`${s.name} latest check`}>
        {facts.length === 0 ? <li className="text-faint">No check recorded yet</li>
          : facts.map((f) => (
            <li key={f.label} className="min-w-0">
              <StatusText status={f.status} label={f.label} className={f.status === "operational" ? "" : "text-ink"} />
              {f.detail && <span className="block truncate pl-3.5 font-mono text-[11px] text-faint" title={f.detail}>{f.detail}</span>}
            </li>
          ))}
      </ul>
      <p className="tabular mt-2 text-xs text-muted lg:mt-0" title={s.current.last_checked_at ?? ""}>
        <span className="lg:hidden">Last checked </span>{ago(s.current.last_checked_at, now)}
        {s.current.stale && <span className="block text-warn">Check overdue</span>}
      </p>
      <div className="mt-3 min-w-0 lg:mt-0">
        {s.bars ? <UptimeBars bars={s.bars} window={window} height={20} compact label={`${s.name} history, last ${window}`} />
          : <p className="text-xs text-faint">No history yet</p>}
        <p className="tabular mt-1 flex justify-between text-[11px] text-faint">
          <span>{s.uptime.uptime_pct === null ? "No data" : !s.uptime.sufficient ? "Insufficient history" : `${pct(s.uptime.uptime_pct, 3)} uptime`}</span>
          <span>avg {ms(s.uptime.avg_latency_ms)}</span>
        </p>
      </div>
    </li>
  );
}

function Monitor({ m }: { m: Summary["monitoring"] }) {
  const now = useNow();
  const w = m.worker;
  const rows: [string, React.ReactNode][] = [
    ["Monitor", <StatusText key="s" status={m.status === "running" ? "operational" : "degraded"}
      label={m.status === "running" ? "Running" : m.status === "stale" ? "Stale" : "Not started"} />],
    ["Interval", `${m.interval_seconds}s`],
    ["Last cycle", w?.last_cycle_started_at ? `${clock(w.last_cycle_started_at)} · ${ms(w.last_cycle_duration_ms)}` : "—"],
    ["Checks recorded", m.total_checks.toLocaleString()],
    ["Storage", m.storage.label],
  ];
  return (
    <section aria-labelledby="monitor-title">
      <h2 id="monitor-title" className="mb-2 text-sm font-semibold">Monitoring</h2>
      <dl className="panel divide-y divide-line text-sm">
        {rows.map(([k, v]) => (
          <div key={k} className="flex items-center justify-between gap-4 px-4 py-2">
            <dt className="text-muted">{k}</dt>
            <dd className="tabular truncate text-right text-ink">{v}</dd>
          </div>
        ))}
      </dl>
      <p className="mt-2 text-xs text-faint">{m.message}{w?.heartbeat_at ? ` Heartbeat ${ago(w.heartbeat_at, now)}.` : ""}</p>
    </section>
  );
}

export default function Overview() {
  const [window, setWindow] = useState<Window>("24h");
  const { data, meta, error, updatedAt, reload } = usePoll<Summary>(`/api/v1/summary?window=${window}`, STATUS_POLL_MS);

  return (
    <Shell meta={meta} updatedAt={updatedAt} error={error} intervalMs={STATUS_POLL_MS}>
      {!data && error ? (
        <ErrorPanel error={error} onRetry={reload} />
      ) : !data ? (
        <div className="space-y-4"><Skeleton className="h-10" /><Skeleton className="h-48" /></div>
      ) : (
        <div className="space-y-8">
          <header className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2" aria-live="polite">
            <div>
              <h1 className="text-lg font-semibold">Service status</h1>
              <p className="mt-0.5 text-sm">
                <StatusText status={data.overall.status} label={data.overall.label} className="font-medium" />
                {data.overall.affected_services.length > 0 && <span className="text-muted"> · {data.overall.affected_services.join(", ")}</span>}
              </p>
            </div>
            <p className="text-xs text-muted">{data.services.length} services · checked every {data.monitoring.interval_seconds}s</p>
          </header>

          <Onboarding summary={data} />

          <section aria-labelledby="services-title">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-3">
              <h2 id="services-title" className="text-sm font-semibold">Services</h2>
              <div className="flex flex-wrap items-center gap-4">
                <span className="hidden md:block"><StatusLegend /></span>
                <WindowTabs value={window} onChange={setWindow} />
              </div>
            </div>
            <div className="panel">
              <div className={`hidden border-b border-line px-5 py-2 text-[11px] font-medium text-faint ${COLS}`} aria-hidden="true">
                <span>Service</span><span>Latest check</span><span>Last checked</span><span>History · {data.window}</span>
              </div>
              {data.services.length === 0 ? (
                <p className="px-5 py-4 text-sm text-muted">No sites yet. <Link href="/sites" className="text-accent hover:underline">Add the first site to monitor</Link>.</p>
              ) : <ul>{data.services.map((s) => <ServiceRow key={s.id} s={s} window={data.window} />)}</ul>}
            </div>
          </section>

          {data.repositories.length > 0 && <Repositories repos={data.repositories} activity={data.agent_activity} />}

          <IncidentList title="Active incidents" incidents={data.active_incidents} empty="No active incidents." />

          <div className="grid items-start gap-8 lg:grid-cols-[1fr_320px]">
            <IncidentList title="Recently resolved" incidents={data.recent_incidents} empty="No resolved incidents yet." />
            <Monitor m={data.monitoring} />
          </div>
        </div>
      )}
    </Shell>
  );
}

/** Mapped repositories: health from Better Stack, and the repair agent's switch and current work. */
function Repositories({ repos, activity }: { repos: RepoOverview[]; activity: AgentActivity[] }) {
  return (
    <section aria-labelledby="repos-title">
      <div className="mb-2 flex items-center justify-between gap-3">
        <h2 id="repos-title" className="text-sm font-semibold">Repositories</h2>
        <Link href="/repositories" className="text-xs text-accent hover:underline">Manage</Link>
      </div>
      <ul className="panel divide-y divide-line">
        {repos.map((r) => {
          const run = activity.find((a) => a.incident_id === r.open_incident_id);
          return (
            <li key={r.service_id} className="flex flex-wrap items-center gap-x-6 gap-y-1 px-5 py-3 text-sm">
              <span className="min-w-40">
                <span className="block font-medium">{r.service_name}</span>
                <span className="font-mono text-xs text-faint">{r.repo}</span>
              </span>
              <StatusText status={r.health} label={r.health === "unknown" ? "No monitor mapped" : undefined} />
              <span className="text-xs text-muted">
                Agent: {r.agent.enabled ? { diagnose: "on, diagnoses failures", draft_pr: "on, opens draft PRs", report_only: "on, report only" }[r.agent.mode] : "off"}
              </span>
              {r.open_incident_id && (
                <Link href={`/incidents/${encodeURIComponent(r.open_incident_id)}`} className="ml-auto text-xs text-accent hover:underline">
                  Open incident{run ? ` · ${STATES[run.state].label}` : ""}
                </Link>
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}
