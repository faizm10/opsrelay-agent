"use client";

import Link from "next/link";
import { use, useState } from "react";
import { ArrowLeft, CheckCircle2, FlaskConical, Globe, Server, Settings2, XCircle } from "lucide-react";
import { LatencyChart, StatusLegend, UptimeBars } from "@/components/charts";
import { IncidentRow } from "@/components/incidents";
import { Badge, ErrorPanel, Shell, Skeleton, Stat, StatusIcon, StatusPill, WindowTabs } from "@/components/ui";
import { UptimeFigure, uptimeBasis } from "@/components/uptime";
import { type Bar, type Check, type LatencyPoint, type Probe, type ServiceDetail, type UptimeStats, type Window, usePoll } from "@/lib/api";
import { FAILURE_LABELS, STATUS_META, ago, clock, duration, ms, useNow } from "@/lib/format";

function ProbeCell({ probe, redact = false }: { probe: Probe | null; redact?: boolean }) {
  if (!probe) return <span className="text-faint">—</span>;
  return (
    <span className="inline-flex items-center gap-1.5" title={redact ? undefined : probe.error ?? probe.url}>
      {probe.ok ? <CheckCircle2 className="h-3.5 w-3.5 text-ok" /> : <XCircle className="h-3.5 w-3.5 text-crit" />}
      <span className="text-ink">{probe.http_status ?? probe.error_kind?.replace("_", " ")}</span>
      <span className="text-faint">{ms(probe.latency_ms)}</span>
    </span>
  );
}

function FunctionalResult({ check }: { check: Check | undefined }) {
  const f = check?.functional;
  if (!f) return <p className="text-sm text-muted">No functional probe results yet.</p>;
  if (f.details.demo_outage) return <p className="text-sm text-muted">Service status: {check?.status}.</p>;
  const failures = f.details.failures ?? [];
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 text-sm">
        {f.ok ? <CheckCircle2 className="h-4 w-4 text-ok" /> : <XCircle className="h-4 w-4 text-crit" />}
        <span className="font-medium">{f.ok ? "Probe healthy" : "Probe failing"}</span>
        <span className="text-muted">· {f.details.summary ?? f.error}</span>
        <span className="ml-auto text-xs text-faint">{clock(check!.checked_at, true)} · HTTP {f.http_status ?? "—"} · {ms(f.latency_ms)}</span>
      </div>
      <p className="mt-1 font-mono text-[11px] text-faint">{f.url}</p>
      {failures.length > 0 && (
        <ul className="mt-4 space-y-3">
          {failures.map((x) => (
            <li key={x.check} className="rounded-lg border border-crit/25 bg-crit/5 p-3.5 text-sm">
              <p className="flex flex-wrap items-center gap-2 font-medium">
                <span className="font-mono text-[13px]">{x.check}</span>
                {x.component && <Badge tone="neutral">{x.component}</Badge>}
              </p>
              <p className="mt-1 text-muted">{x.reason}</p>
              {x.source && <p className="mt-1.5 font-mono text-[11.5px] text-accent">{x.source}</p>}
              {(x.expected !== undefined || x.actual !== undefined) && (
                <div className="scroll-x mt-2 grid gap-1 font-mono text-[11.5px]">
                  <span className="whitespace-nowrap text-ok">expected {JSON.stringify(x.expected)}</span>
                  <span className="whitespace-nowrap text-crit">actual&nbsp;&nbsp; {JSON.stringify(x.actual)}</span>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function UptimeTiles({ uptime }: { uptime: Record<Window, UptimeStats> }) {
  return (
    <>
      {(["24h", "7d", "30d"] as Window[]).map((w) => {
        const u = uptime[w];
        return (
          <Stat
            key={w}
            label={`Uptime · ${w}`}
            value={<UptimeFigure stats={u} big />}
            sub={u.sufficient ? `availability ${u.availability_pct?.toFixed(2)}%` : uptimeBasis(u)}
          />
        );
      })}
    </>
  );
}

export default function ServicePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [window, setWindow] = useState<Window>("24h");
  const detail = usePoll<ServiceDetail>(`/api/v1/services/${id}`);
  const uptime = usePoll<{ stats: UptimeStats; bars: Bar[] }>(`/api/v1/services/${id}/uptime?window=${window}`);
  const latency = usePoll<LatencyPoint[]>(`/api/v1/services/${id}/latency?window=${window}&points=120`);
  const checks = usePoll<Check[]>(`/api/v1/services/${id}/checks?limit=25`);
  const now = useNow();
  const d = detail.data;

  return (
    <Shell meta={detail.meta} updatedAt={detail.updatedAt} error={detail.error}>
      <Link href="/" className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink">
        <ArrowLeft className="h-4 w-4" /> All services
      </Link>
      {!d && detail.error ? (
        <ErrorPanel error={detail.error} onRetry={detail.reload} />
      ) : !d ? (
        <div className="mt-6 space-y-5">
          <Skeleton className="h-36" />
          <Skeleton className="h-64" />
        </div>
      ) : (
        <div className="mt-5 space-y-6">
          <section className="panel p-6 sm:p-7">
            <div className="flex flex-wrap items-start gap-4">
              <StatusIcon status={d.current.status} className="h-8 w-8" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2.5">
                  <h1 className="page-title">{d.service.name}</h1>
                  <StatusPill status={d.current.status} />
                </div>
                <p className="mt-1 text-sm text-muted">{d.service.description}</p>
                <p className="mt-1 text-xs text-faint">
                  {d.current.since && `${STATUS_META[d.current.status].label} since ${clock(d.current.since, true)} · `}
                  last checked {ago(d.current.last_checked_at, now)} · last success {ago(d.current.last_success_at, now)}
                </p>
              </div>
            </div>
            <div className="mt-6 grid grid-cols-2 gap-5 border-t border-line pt-5 md:grid-cols-5">
              <UptimeTiles uptime={d.uptime} />
              <Stat label="Avg latency · 24h" value={ms(d.uptime["24h"].avg_latency_ms)} sub={`p95 ${ms(d.uptime["24h"].p95_latency_ms)}`} />
              <Stat label="Probe latency · 24h" value={ms(d.uptime["24h"].avg_functional_latency_ms)} sub="functional probe" />
            </div>
          </section>

          <section className="panel p-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="section-title">Uptime history</h2>
              <WindowTabs value={window} onChange={setWindow} />
            </div>
            <div className="mt-6">
              {uptime.data ? <UptimeBars bars={uptime.data.bars} window={window} height={48} label={`${d.service.name} uptime`} /> : <Skeleton className="h-12" />}
            </div>
            <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
              <StatusLegend />
              {uptime.data && (
                <p className="tabular text-xs text-muted">
                  {uptime.data.stats.check_count} checks · {uptime.data.stats.failed_check_count} failed · down {duration(uptime.data.stats.down_seconds)} · degraded{" "}
                  {duration(uptime.data.stats.degraded_seconds)} · no data {duration(uptime.data.stats.unknown_seconds)}
                </p>
              )}
            </div>
          </section>

          <section className="panel grid gap-8 p-6 lg:grid-cols-2">
            {latency.data ? (
              <>
                <LatencyChart title="Availability endpoint latency" points={latency.data.map((p) => ({ t: p.start, avg: p.availability_avg_ms, max: p.availability_max_ms, samples: p.samples }))} />
                <LatencyChart title="Functional probe latency" points={latency.data.map((p) => ({ t: p.start, avg: p.functional_avg_ms, max: p.functional_max_ms, samples: p.samples }))} />
              </>
            ) : (
              <>
                <Skeleton className="h-44" />
                <Skeleton className="h-44" />
              </>
            )}
          </section>

          <section className="panel p-6">
            <h2 className="mb-4 flex items-center gap-2 text-sm font-semibold"><FlaskConical className="h-4 w-4 text-muted" /> Latest functional probe</h2>
            <FunctionalResult check={checks.data?.find((c) => c.functional)} />
          </section>

          <section className="panel p-6">
            <h2 className="section-title">Latest checks</h2>
            <div className="scroll-x mt-4">
              <table className="tabular w-full min-w-[720px] text-left text-sm">
                <thead className="text-[11px] uppercase tracking-wider text-faint">
                  <tr className="border-b border-line">
                    <th className="py-2 pr-4 font-semibold">Time</th>
                    <th className="py-2 pr-4 font-semibold">Result</th>
                    <th className="py-2 pr-4 font-semibold"><Globe className="mr-1 inline h-3 w-3" />Website</th>
                    <th className="py-2 pr-4 font-semibold"><Server className="mr-1 inline h-3 w-3" />Availability</th>
                    <th className="py-2 pr-4 font-semibold"><FlaskConical className="mr-1 inline h-3 w-3" />Functional</th>
                    <th className="py-2 font-semibold">Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {(checks.data ?? []).map((c) => (
                    <tr key={c.id} className="border-b border-line/60 last:border-0 hover:bg-hover/60">
                      <td className="whitespace-nowrap py-2.5 pr-4 text-muted">
                        {clock(c.checked_at, true)}
                        {!c.scheduled && <span className="ml-1.5 rounded bg-raised px-1 text-[10px] text-faint">manual</span>}
                      </td>
                      <td className="py-2.5 pr-4"><StatusPill status={c.status} /></td>
                      <td className="py-2.5 pr-4"><ProbeCell probe={c.frontend} redact={c.functional?.details.demo_outage} /></td>
                      <td className="py-2.5 pr-4"><ProbeCell probe={c.availability} redact={c.functional?.details.demo_outage} /></td>
                      <td className="py-2.5 pr-4"><ProbeCell probe={c.functional} redact={c.functional?.details.demo_outage} /></td>
                      <td className="max-w-[260px] truncate py-2.5 text-xs text-muted" title={c.functional?.details.demo_outage ? undefined : c.summary}>
                        {c.functional?.details.demo_outage ? STATUS_META[c.status].label : c.failure_type ? FAILURE_LABELS[c.failure_type] ?? c.failure_type : "All healthy"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {checks.data?.length === 0 && <p className="py-6 text-center text-sm text-muted">No checks recorded yet.</p>}
            </div>
          </section>

          <div className="grid items-start gap-6 lg:grid-cols-[1fr_360px]">
            <section className="panel p-6">
              <h2 className="section-title">Incidents & outages</h2>
              {d.incidents.length === 0 ? (
                <p className="mt-4 text-sm text-muted">No incidents recorded for {d.service.name}.</p>
              ) : (
                <ul className="mt-4 divide-y divide-line border-t border-line">{d.incidents.map((i) => <IncidentRow key={i.id} incident={i} />)}</ul>
              )}
            </section>
            <section className="panel p-6">
              <h2 className="flex items-center gap-2 text-sm font-semibold"><Settings2 className="h-4 w-4 text-muted" /> Monitoring configuration</h2>
              <dl className="mt-4 space-y-2.5 text-sm">
                {[
                  ["Website", d.service.frontend_url],
                  ["Availability", `GET ${d.service.backend_url}${d.service.availability_path}`],
                  ["Functional probe", `${d.service.functional_method} ${d.service.backend_url}${d.service.functional_path}`],
                  ["Interval", `${d.service.interval_seconds}s`],
                  ["Timeouts", `${d.service.timeout_seconds}s availability · ${d.service.functional_timeout_seconds}s probe`],
                  ["Incident opens after", `${d.service.failure_threshold} failing check${d.service.failure_threshold > 1 ? "s" : ""}`],
                  ["Resolves after", `${d.service.recovery_threshold} healthy check${d.service.recovery_threshold > 1 ? "s" : ""}`],
                  ["Monitoring", d.service.enabled ? "Enabled" : "Paused"],
                  ["Agent app key", d.service.app],
                ].map(([k, v]) => (
                  <div key={k} className="grid grid-cols-[130px_1fr] gap-3">
                    <dt className="text-muted">{k}</dt>
                    <dd className="break-all font-mono text-[12px] text-ink">{v}</dd>
                  </div>
                ))}
              </dl>
            </section>
          </div>
        </div>
      )}
    </Shell>
  );
}
