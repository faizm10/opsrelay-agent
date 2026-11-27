"use client";

import Link from "next/link";
import { use, useState } from "react";
import { ArrowLeft, ArrowUpRight, GitPullRequest } from "lucide-react";
import { toast } from "sonner";
import { Badge, ErrorPanel, Shell, Skeleton, StatusText } from "@/components/ui";
import { type Incident, type Walkthrough, isDemoIncident, send, usePoll } from "@/lib/api";
import { FAILURE_LABELS, clock, duration, useNow } from "@/lib/format";
import { agentRun, healthAndProgress, phase } from "@/lib/phase";
import { plainFailure } from "@/lib/plain";

type Handoff = Record<string, unknown> & {
  probe_failures: { check: string; reason: string; source?: string | null; expected?: unknown; actual?: unknown }[];
  suspect_sources: string[];
  next_steps: string[];
  code_repair_candidate: boolean;
};
type Rec = { action?: string; failure_point?: string; cause?: string; detail?: string; evidence?: string[] };
type Verification = { tests_passed?: boolean; test_output?: string; verified_at?: string };

const RUN_STATUS: Record<string, string> = {
  open: "Investigating",
  awaiting_merge: "Awaiting PR review",
  needs_review: "Needs an engineer",
  failed: "Stopped after repeated errors",
  reported: "Proposed fix ready (report-only)",
  awaiting_deployment: "Awaiting deployment",
  resolved: "Merged",
  rejected: "PR closed without merge",
};

const ACTION: Record<string, string> = {
  propose_code_fix: "Code fix",
  operational_step: "Operational step",
  ask_for_review: "Needs an engineer",
};

const EVENT_LABEL: Record<string, string> = {
  opened: "Incident opened",
  escalated: "Escalated",
  deescalated: "De-escalated",
  failure_changed: "Failure changed",
  resolved: "Incident resolved",
  acknowledged: "Acknowledged",
  link_added: "Link added",
  fix_confirmed: "Fix confirmed",
  fix_disputed: "Still failing after the fix",
  fix_merged: "Fix marked as merged",
  service_recovered: "Fresh check passed",
  relapsed: "Failing again",
};

type Entry = { at: string; label: string; detail?: string; source: string };

/** Monitor and agent records as one chronological list. Nothing is inferred. */
function timeline(i: Incident, walk: Walkthrough | null, demo: boolean): Entry[] {
  const out: Entry[] = i.events.map((e) => ({ at: e.at, label: EVENT_LABEL[e.type] ?? e.type, detail: demo ? undefined : e.message, source: "Monitor" }));
  for (const s of walk?.steps ?? []) {
    if (!s.ended_at || s.state === "waiting" || s.state === "running") continue;
    const d = s.details as Record<string, unknown>;
    const label = {
      similar: `Similar incidents retrieved (${(d.matches as unknown[] | undefined)?.length ?? 0})`,
      investigated: s.state === "done" ? "Patch verified" : "Investigation stopped: needs an engineer",
      pr: d.recovered_existing ? `Existing PR found after restart${d.pr_number ? ` (#${d.pr_number})` : ""}` : `Draft PR opened${d.pr_number ? ` (#${d.pr_number})` : ""}`,
      merge: s.state === "failed" ? "PR closed without merge" : "PR merged",
    }[s.key];
    if (label) out.push({ at: s.ended_at, label, detail: s.key === "investigated" ? s.summary : undefined, source: "Investigation" });
  }
  return out.sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime());
}

type FailedDeploy = { id: string; sha: string | null; deployed_at: string | null; inspector_url: string | null };
type Diagnosis = { summary: string; likely_cause: string; suggested_fix: string; confidence: "low" | "medium" | "high"; created_at: string };
type BacklogItem = { number: number; kind: "pull_request" | "issue"; title: string; author: string | null; labels: string[];
  url: string | null; opened_at: string | null };
type FailedRun = { id: number; workflow: string; sha: string | null; conclusion: string; url: string | null; at: string | null };
type SentryIssue = { id: string; short_id: string | null; title: string; culprit: string | null; level: string; url: string | null };
type DependabotAlert = { number: number; package: string; severity: string; summary: string; manifest: string | null;
  patched_version: string | null; url: string | null };

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0 px-4 py-3 sm:px-5">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm text-ink">{children}</dd>
    </div>
  );
}

function RunAgent({ id, requestedAt, again, onQueued }: { id: string; requestedAt?: string | null; again: boolean; onQueued: () => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const queue = async () => {
    setPending(true);
    setError("");
    try {
      await send("POST", `/api/v1/incidents/${encodeURIComponent(id)}/diagnosis`);
      toast.success("Agent run queued", { description: "It picks this incident up within about 5 minutes." });
      onQueued();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-3 border-t border-line px-4 py-3 text-sm sm:px-5">
      {requestedAt ? (
        <span className="text-muted">Queued {clock(requestedAt, true)}. The agent picks it up on its next run, within about 5 minutes.</span>
      ) : (
        <button type="button" className="btn" disabled={pending} onClick={queue}>{again ? "Run the agent again" : "Run the agent now"}</button>
      )}
      {error && <span className="text-crit" role="alert">{error}</span>}
    </div>
  );
}

function Section({ title, aside, children }: { title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  const id = `${title.toLowerCase().replace(/\W+/g, "-")}-title`;
  return (
    <section aria-labelledby={id}>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id={id} className="text-sm font-semibold">{title}</h2>
        {aside && <span className="text-xs text-muted">{aside}</span>}
      </div>
      <div className="panel">{children}</div>
    </section>
  );
}

function More({ summary, children }: { summary: string; children: React.ReactNode }) {
  return (
    <details className="group border-t border-line px-4 py-2.5 text-sm sm:px-5">
      <summary className="cursor-pointer text-xs font-medium text-muted hover:text-ink">{summary}</summary>
      <div className="mt-2">{children}</div>
    </details>
  );
}

/** Long lists show the first few items; the rest fold away. */
function Capped<T>({ items, render, show = 5 }: { items: T[]; render: (item: T) => React.ReactNode; show?: number }) {
  return (
    <>
      <ul className="space-y-2 text-sm">{items.slice(0, show).map(render)}</ul>
      {items.length > show && (
        <details className="group mt-3 text-sm">
          <summary className="cursor-pointer text-xs font-medium text-muted hover:text-ink">Show {items.length - show} more</summary>
          <ul className="mt-2 space-y-2">{items.slice(show).map(render)}</ul>
        </details>
      )}
    </>
  );
}

const pre = "scroll-x max-h-80 overflow-y-auto rounded-md border border-line bg-page p-3 font-mono text-[11px] leading-relaxed text-muted";

export default function IncidentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const inc = usePoll<Incident>(`/api/v1/incidents/${encodeURIComponent(id)}`);
  const i = inc.data;
  const demo = i ? isDemoIncident(i) : false;
  const handoff = usePoll<Handoff>(i && !demo ? `/api/v1/incidents/${encodeURIComponent(id)}/evidence` : null);
  const walk = usePoll<Walkthrough>(i ? `/api/v1/incidents/${encodeURIComponent(id)}/walkthrough` : null);
  const diagnosis = usePoll<Diagnosis | null>(i && !demo ? `/api/v1/incidents/${encodeURIComponent(id)}/diagnosis` : null);
  const now = useNow();

  return (
    <Shell meta={inc.meta} updatedAt={inc.updatedAt} error={inc.error}>
      <Link href="/incidents" className="inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink">
        <ArrowLeft className="h-4 w-4" aria-hidden="true" /> Incidents
      </Link>
      {!i && inc.error ? (
        <ErrorPanel error={inc.error} onRetry={inc.reload} />
      ) : !i ? (
        <Skeleton className="mt-6 h-48" />
      ) : (() => {
        const run = agentRun(i);
        // Diagnose mode writes a diagnosis without a repair run; that is the agent's answer for this incident.
        const agent = i.repo_agent;
        const waiting = !run && !diagnosis.data && i.state === "open";
        const agentText = agent?.enabled
          ? `The agent is on for this repository${agent.mode === "diagnose" ? " (diagnose mode)" : ""}. Its findings appear here after its next run.`
          : "The agent is off for this repository.";
        const p = diagnosis.data && !run && i.state === "open"
          ? { label: "Diagnosed", next: "Read the likely cause and suggested fix below", tone: "warn" as const }
          : waiting && agent ? { label: agent.enabled ? "Waiting for the agent" : "Agent off",
              next: agent.enabled ? agentText : `${agentText} Switch it on under Repositories.`, tone: "neutral" as const }
          : phase(i);
        const hp = healthAndProgress(i);
        const steps = Object.fromEntries((walk.data?.steps ?? []).map((s) => [s.key, s]));
        const inv = steps.investigated;
        const invDetails = (inv?.details ?? {}) as Record<string, unknown>;
        const rec = invDetails.recommendation as Rec | undefined;
        const ver = invDetails.verification as Verification | undefined;
        const prUrl = ((steps.pr?.details as Record<string, unknown> | undefined)?.url as string | undefined) ?? run?.pr_url ?? i.links.find((l) => l.kind === "repair_pr")?.url;
        const elapsed = i.state === "open" ? (now - new Date(i.started_at).getTime()) / 1000 : i.duration_seconds;
        const entries = timeline(i, walk.data, demo);
        const others = i.related_ci.filter((c) => c.id !== run?.id);
        const lastTestLine = ver?.test_output?.trim().split("\n").pop();
        // A stopped investigation records tests_passed=false with no patch: nothing was tested.
        const noPatch = !(invDetails.changed_files as string[] | undefined)?.length;
        const ranRepair = !!inv && !["waiting", "running", "skipped"].includes(inv.state);
        const alert = i.evidence.source === "betterstack" ? i.evidence as { cause?: string; url?: string; response_url?: string; screenshot_url?: string } : null;
        const deps = i.evidence.source === "dependabot" ? (i.evidence.alerts as DependabotAlert[]) : null;
        const issues = i.evidence.source === "sentry" ? (i.evidence.issues as SentryIssue[]) : null;
        const deploys = i.evidence.source === "vercel" ? (i.evidence.deployments as FailedDeploy[]) : null;
        const runs = i.evidence.source === "actions" ? (i.evidence.runs as FailedRun[]) : null;
        const backlog = i.evidence.source === "backlog" ? (i.evidence.items as BacklogItem[]) : null;
        const source = alert ? `Better Stack · ${alert.cause ?? "alert"}`
          : deps ? `Dependabot · ${deps.length} open alert${deps.length === 1 ? "" : "s"}`
          : issues ? `Sentry · ${issues.length} unresolved issue${issues.length === 1 ? "" : "s"}`
          : deploys ? "Vercel · production deploy failed"
          : runs ? `GitHub Actions · ${runs.map((r) => r.workflow).join(", ")}`
          : backlog ? `GitHub · ${backlog.length} open pull request${backlog.length === 1 ? "" : "s"} and issue${backlog.length === 1 ? "" : "s"}`
          : `Functional check · ${FAILURE_LABELS[i.failure_type ?? ""] ?? i.failure_type ?? "failing"}`;

        return (
          <div className="mt-4 space-y-8">
            <header>
              <p className="text-sm text-muted">{i.service_name}</p>
              <h1 className="page-title mt-1">{i.title}</h1>
              <p className="mt-1 text-sm text-muted">
                <StatusText status={i.service_health} label={hp.health} />
                <span className="tabular"> · {i.state === "open" ? "open for" : "lasted"} {duration(elapsed)} · started {clock(i.started_at, true)}{i.resolved_at ? ` · resolved ${clock(i.resolved_at, true)}` : ""}</span>
              </p>
              <p className="mt-1 font-mono text-[11px] text-faint">{i.id}</p>
            </header>

            <section aria-label="Next step" className="panel flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:px-5">
              <Badge tone={p.tone}>{p.label}</Badge>
              <p className="min-w-0 flex-1 text-sm text-ink">{p.next}</p>
              {prUrl && (
                <a href={prUrl} target="_blank" rel="noreferrer" className="btn">
                  <GitPullRequest className="h-3.5 w-3.5" aria-hidden="true" /> View pull request <ArrowUpRight className="h-3 w-3" aria-hidden="true" />
                </a>
              )}
            </section>

            {!demo && (
              <Section title="What's wrong" aside={source}>
                <div className="px-4 py-4 sm:px-5">
                  {alert ? (
                    <div className="space-y-1 text-sm">
                      <p className="font-medium text-ink">Better Stack: {alert.cause ?? "alert"}</p>
                      {alert.url && <p className="break-all text-xs text-muted">Monitored URL: {alert.url}</p>}
                      {[["Response", alert.response_url], ["Screenshot", alert.screenshot_url]].map(([label, url]) => url?.startsWith("https://") && (
                        <a key={label} href={url} target="_blank" rel="noreferrer" className="mr-3 text-xs text-accent hover:underline">{label}</a>
                      ))}
                    </div>
                  ) : deps ? (
                    <Capped items={deps} render={(d) => (
                      <li key={d.number}>
                        <p className="font-medium text-ink">
                          {d.package} <span className="text-xs font-normal text-muted">{d.severity}{d.patched_version ? ` · fixed in ${d.patched_version}` : " · no fix yet"}</span>
                        </p>
                        <p className="text-muted">{d.summary}</p>
                        <p className="font-mono text-xs text-faint">
                          {d.manifest}{d.url?.startsWith("https://") && <> · <a href={d.url} target="_blank" rel="noreferrer" className="font-sans text-accent hover:underline">Alert #{d.number}</a></>}
                        </p>
                      </li>
                    )} />
                  ) : issues ? (
                    <Capped items={issues} render={(x) => (
                      <li key={x.id}>
                        <p className="font-medium text-ink">
                          {x.url?.startsWith("https://") ? <a href={x.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">{x.short_id ?? x.id}</a> : x.short_id ?? x.id}
                          {" "}<span className="text-xs font-normal text-muted">{x.level}</span>
                        </p>
                        <p className="text-muted">{x.title}</p>
                        {x.culprit && <p className="font-mono text-xs text-faint">{x.culprit}</p>}
                      </li>
                    )} />
                  ) : deploys ? (
                    <Capped items={deploys} render={(d) => (
                      <li key={d.id}>
                        <p className="font-medium text-ink">Deploy {d.sha ? <span className="font-mono">{d.sha.slice(0, 7)}</span> : d.id} failed to build</p>
                        <p className="text-xs text-muted">
                          {d.deployed_at ? new Date(d.deployed_at).toLocaleString() : ""}
                          {d.inspector_url?.startsWith("https://") && <> · <a href={d.inspector_url} target="_blank" rel="noreferrer" className="text-accent hover:underline">Build logs</a></>}
                        </p>
                      </li>
                    )} />
                  ) : runs ? (
                    <Capped items={runs} render={(r) => (
                      <li key={r.id}>
                        <p className="font-medium text-ink">{r.workflow} {r.conclusion === "timed_out" ? "timed out" : "failed"}{r.sha && <> at <span className="font-mono">{r.sha.slice(0, 7)}</span></>}</p>
                        <p className="text-xs text-muted">
                          {r.at ? new Date(r.at).toLocaleString() : ""}
                          {r.url?.startsWith("https://") && <> · <a href={r.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">Run logs</a></>}
                        </p>
                      </li>
                    )} />
                  ) : backlog ? (
                    <Capped items={backlog} render={(b) => (
                      <li key={b.number}>
                        <p className="font-medium text-ink">
                          {b.url?.startsWith("https://") ? <a href={b.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">#{b.number}</a> : `#${b.number}`}
                          {" "}{b.title}
                        </p>
                        <p className="text-xs text-muted">
                          {b.kind === "pull_request" ? "Pull request" : "Issue"}{b.author ? ` by ${b.author}` : ""}
                          {b.opened_at ? ` · opened ${new Date(b.opened_at).toLocaleDateString()}` : ""}{b.labels.length ? ` · ${b.labels.join(", ")}` : ""}
                        </p>
                      </li>
                    )} />
                  ) : handoff.data?.probe_failures.length ? (
                    <ul className="space-y-3">
                      {handoff.data.probe_failures.map((f) => {
                        const plain = plainFailure(f);
                        return (
                          <li key={f.check} className="text-sm">
                            <p className="font-medium text-ink">{plain.title} <span className="font-mono text-xs font-normal text-faint">{f.check}</span></p>
                            <p className="mt-0.5 text-muted">{plain.explanation}</p>
                            {plain.rows.length > 0 && (
                              <div className="scroll-x mt-2">
                                <table className="tabular w-full min-w-[300px] text-left text-xs">
                                  <thead className="text-faint"><tr><th className="py-1 pr-4 font-medium">Case</th><th className="py-1 pr-4 font-medium">Expected</th><th className="py-1 font-medium">Actual</th></tr></thead>
                                  <tbody>
                                    {plain.rows.map((row) => (
                                      <tr key={row.label} className="border-t border-line">
                                        <td className="py-1.5 pr-4 text-muted">{row.label}</td>
                                        <td className="py-1.5 pr-4 text-ink">{row.expected}</td>
                                        <td className="py-1.5 font-medium text-crit">{row.actual}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )}
                          </li>
                        );
                      })}
                    </ul>
                  ) : <p className="text-sm text-muted">{i.summary}</p>}
                </div>
                {handoff.data && (
                  <More summary="Raw incident evidence (JSON)">
                    {i.evidence.route_reason && <p className="mb-2 text-xs text-muted">{i.evidence.route_reason}</p>}
                    <pre className={pre}>{JSON.stringify(handoff.data, null, 2)}</pre>
                  </More>
                )}
              </Section>
            )}

            {!demo && (
              <Section title="What the agent found"
                aside={run ? RUN_STATUS[run.status] ?? run.status : diagnosis.data ? `${diagnosis.data.confidence} confidence · ${clock(diagnosis.data.created_at, true)}` : undefined}>
                {diagnosis.data && (
                  <dl className="divide-y divide-line">
                    <Field label="Likely cause"><span className="whitespace-pre-line">{diagnosis.data.likely_cause}</span></Field>
                    <Field label="Suggested fix"><span className="whitespace-pre-line">{diagnosis.data.suggested_fix}</span></Field>
                  </dl>
                )}
                {ranRepair ? (
                  <dl className={`divide-y divide-line ${diagnosis.data ? "border-t border-line" : ""}`}>
                    {rec?.action && <Field label="Recommendation">{ACTION[rec.action] ?? rec.action}{rec.detail ? ` · ${rec.detail}` : ""}</Field>}
                    {(invDetails.root_cause as string | undefined) && <Field label="Cause">{invDetails.root_cause as string}</Field>}
                    <Field label={noPatch ? "Why no patch" : "Proposed change"}>{inv.summary}</Field>
                    {!noPatch && <Field label="Files"><span className="font-mono text-xs">{(invDetails.changed_files as string[]).join(", ")}</span></Field>}
                    {ver?.tests_passed != null && !noPatch && (
                      <Field label="Tests">
                        <StatusText status={ver.tests_passed ? "operational" : "down"} label={ver.tests_passed ? "Tests passed with the fix" : "Tests failed with the fix"} />
                        {lastTestLine && <span className="block truncate font-mono text-xs text-muted" title={lastTestLine}>{lastTestLine}</span>}
                      </Field>
                    )}
                  </dl>
                ) : !diagnosis.data && (
                  <p className="px-4 py-4 text-sm text-muted sm:px-5">
                    {inv?.state === "skipped" ? "Not a code problem, so no fix is proposed."
                      : run && inv?.summary ? inv.summary
                      : agent ? <>{agentText}{!agent.enabled && <> Switch it on under <Link href="/repositories" className="text-accent hover:underline">Repositories</Link>.</>}</>
                      : <>Nothing yet. This service has no repository mapped, so the agent can&apos;t look at it. Map one under{" "}
                        <Link href="/repositories" className="text-accent hover:underline">Repositories</Link>.</>}
                  </p>
                )}
                {agent?.enabled && agent.mode === "diagnose" && !run && i.state === "open" && (
                  <RunAgent id={i.id} requestedAt={i.diagnosis_requested_at} again={!!diagnosis.data}
                    onQueued={() => { inc.reload(); diagnosis.reload(); }} />
                )}
                {ver?.test_output && <More summary="Test output"><pre className={pre}>{ver.test_output}</pre></More>}
                {!!rec?.evidence?.length && (
                  <More summary={`Evidence the agent used (${rec.evidence.length})`}>
                    <ul className="list-disc space-y-1 pl-4 text-xs text-muted">{rec.evidence.map((e) => <li key={e}>{e}</li>)}</ul>
                    {rec.failure_point && <p className="mt-2 text-xs text-muted">Failure point: {rec.failure_point}</p>}
                  </More>
                )}
                {!!(steps.similar?.details as { matches?: unknown[] } | undefined)?.matches?.length && (
                  <More summary="Similar past incidents">
                    <ul className="tabular space-y-1 font-mono text-xs text-muted">
                      {((steps.similar.details as { matches: { memory_id: string; score: number }[] }).matches).map((m) => <li key={m.memory_id}>{m.memory_id} · score {m.score}</li>)}
                    </ul>
                  </More>
                )}
                {others.length > 0 && (
                  <More summary={`Other repair runs (${others.length})`}>
                    <ul className="space-y-1 text-xs text-muted">
                      {others.map((c) => (
                        <li key={c.id} className="flex flex-wrap gap-x-3">
                          <span className="font-mono">{c.id}</span><span>{RUN_STATUS[c.status] ?? c.status}</span>
                          {c.pr_url && <a className="text-accent hover:underline" href={c.pr_url} target="_blank" rel="noreferrer">PR</a>}
                          {c.run_url && <a className="text-accent hover:underline" href={c.run_url} target="_blank" rel="noreferrer">CI run</a>}
                        </li>
                      ))}
                    </ul>
                  </More>
                )}
                {run && (
                  <div className="border-t border-line px-4 py-2.5 sm:px-5">
                    <Link href={`/ai-agent/${encodeURIComponent(i.id)}`} className="text-xs font-medium text-accent hover:underline">Step-by-step investigation</Link>
                  </div>
                )}
              </Section>
            )}

            <Section title="Timeline" aside={`${entries.length} event${entries.length === 1 ? "" : "s"}`}>
              <ol className="divide-y divide-line">
                {entries.map((e, n) => (
                  <li key={n} className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-3 px-4 py-2.5 text-sm sm:grid-cols-[8.5rem_minmax(0,1fr)_10rem] sm:px-5">
                    <time className="tabular text-xs text-muted" dateTime={e.at}>{clock(e.at, true)}</time>
                    <div className="min-w-0">
                      <p className="text-ink">{e.label}</p>
                      {e.detail && <p className="text-xs text-muted">{e.detail}</p>}
                    </div>
                    <p className="col-start-2 truncate text-xs text-faint sm:col-start-auto sm:text-right">{e.source}</p>
                  </li>
                ))}
                {i.state === "open" && (
                  <li className="px-4 py-2.5 text-sm text-muted sm:px-5">Still failing · last failure <span className="tabular">{clock(i.last_failure_at, true)}</span></li>
                )}
              </ol>
            </Section>

          </div>
        );
      })()}
    </Shell>
  );
}
