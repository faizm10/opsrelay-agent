"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";
import { ArrowLeft, ArrowUpRight, Check, Circle, CircleAlert, Minus, X } from "lucide-react";
import { Badge, ErrorPanel, PageHeader, Shell, Skeleton, type Tone } from "@/components/ui";
import { type StepState, type WalkStep, type Walkthrough, usePoll } from "@/lib/api";
import { clock, duration } from "@/lib/format";

const STATE: Record<StepState, { label: string; tone: Tone }> = {
  waiting: { label: "Waiting", tone: "neutral" },
  running: { label: "Running", tone: "accent" },
  done: { label: "Done", tone: "ok" },
  failed: { label: "Failed", tone: "crit" },
  needs_review: { label: "Needs review", tone: "warn" },
  skipped: { label: "Skipped", tone: "neutral" },
};
const HEALTH: Record<Walkthrough["health"]["state"], { label: string; tone: Tone }> = {
  on_track: { label: "On track", tone: "ok" },
  slow: { label: "Slow", tone: "warn" },
  attention: { label: "Needs a person", tone: "crit" },
  done: { label: "Resolved", tone: "ok" },
};
const DOT: Record<StepState, string> = {
  waiting: "border-line-strong bg-page text-faint",
  running: "border-accent bg-accent/15 text-accent",
  done: "border-ok bg-ok/15 text-ok",
  failed: "border-crit bg-crit/15 text-crit",
  needs_review: "border-warn bg-warn/15 text-warn",
  skipped: "border-line bg-page text-faint",
};

function StepIcon({ state }: { state: StepState }) {
  const Icon = { done: Check, failed: X, needs_review: CircleAlert, skipped: Minus, waiting: Circle, running: Circle }[state];
  return (
    <span className={`relative flex h-7 w-7 flex-none items-center justify-center rounded-full border ${DOT[state]}`}>
      {state === "running" && <span className="absolute inset-0 animate-ping2 rounded-full bg-accent/30" />}
      <Icon className="h-3.5 w-3.5" aria-hidden="true" />
    </span>
  );
}

function Pipeline({ steps, selected, onSelect }: { steps: WalkStep[]; selected: string; onSelect: (k: string) => void }) {
  return (
    <ol className="flex flex-col gap-1 lg:flex-row lg:gap-0">
      {steps.map((s, i) => (
        <li key={s.key} className="relative lg:flex-1">
          {i > 0 && <span className={`absolute left-[27px] -top-1 h-2 w-px lg:left-0 lg:top-[27px] lg:h-px lg:w-1/2 ${s.state === "waiting" ? "bg-line" : "bg-ok/50"}`} />}
          {i < steps.length - 1 && <span className={`absolute bottom-[-4px] left-[27px] hidden h-2 w-px lg:left-1/2 lg:top-[27px] lg:block lg:h-px lg:w-1/2 ${steps[i + 1].state === "waiting" ? "bg-line" : "bg-ok/50"}`} />}
          <button type="button" onClick={() => onSelect(s.key)} aria-pressed={selected === s.key}
            className={`relative flex w-full items-center gap-3 rounded-lg p-3 text-left transition lg:flex-col lg:gap-2 lg:text-center ${
              selected === s.key ? "bg-raised" : "hover:bg-raised/60"}`}>
            <StepIcon state={s.state} />
            <span className="min-w-0 flex-1">
              <span className={`block text-sm font-medium lg:text-xs ${s.state === "waiting" ? "text-muted" : "text-ink"}`}>{s.label}</span>
              <span className="tabular block text-xs text-faint lg:text-[11px]">
                {s.duration_seconds != null ? duration(s.duration_seconds) : STATE[s.state].label}
              </span>
            </span>
          </button>
        </li>
      ))}
    </ol>
  );
}

function Rows({ rows }: { rows: [string, React.ReactNode][] }) {
  const shown = rows.filter(([, v]) => v !== undefined && v !== null && v !== "" && v !== false);
  if (!shown.length) return null;
  return (
    <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
      {shown.map(([k, v]) => (
        <div key={k} className="min-w-0"><dt className="label">{k}</dt><dd className="mt-1 wrap-break-word text-ink">{v}</dd></div>
      ))}
    </dl>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="space-y-3 border-t border-line pt-4"><h3 className="text-xs font-semibold text-muted">{title}</h3>{children}</section>;
}

type Rec = { action?: string; failure_point?: string; cause?: string; detail?: string; evidence?: string[] };
type Verification = { tests_passed?: boolean; test_output?: string; verified_at?: string };

function Detail({ step }: { step: WalkStep }) {
  const d = step.details as Record<string, unknown>;
  const rec = d.recommendation as Rec | undefined;
  const ver = d.verification as Verification | undefined;
  const matches = d.matches as { memory_id: string; score: number }[] | undefined;
  const prUrl = (d.url ?? d.pr_url) as string | undefined;
  return (
    <section className="panel space-y-4 p-5 sm:p-6" aria-live="polite">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-base font-semibold">{step.label}</h2>
        <Badge tone={STATE[step.state].tone}>{STATE[step.state].label}</Badge>
      </div>
      <p className="text-sm text-muted">{step.summary}</p>
      <Rows rows={[
        ["Started", step.started_at && clock(step.started_at, true)],
        ["Finished", step.ended_at && clock(step.ended_at, true)],
        ["Duration", step.duration_seconds != null && duration(step.duration_seconds)],
        ["Failure type", d.failure_type as string], ["Failed checks", d.failed_checks as number],
        ["Changed files", (d.changed_files as string[] | undefined)?.join(", ")],
        ["Added tests", (d.added_tests as string[] | undefined)?.join(", ")],
        ["Check", d.check_id as string],
      ]} />
      {rec && (
        <Section title="Recommendation">
          <Rows rows={[["Action", rec.action?.replaceAll("_", " ")], ["Failure point", rec.failure_point], ["Cause", rec.cause]]} />
          {rec.detail && <p className="text-sm text-muted">{rec.detail}</p>}
          {!!rec.evidence?.length && (
            <ul className="card space-y-1 p-3 text-xs text-muted">{rec.evidence.map((e) => <li key={e}>{e}</li>)}</ul>
          )}
        </Section>
      )}
      {ver && ver.tests_passed != null && (
        <Section title="Tests">
          <Badge tone={ver.tests_passed ? "ok" : "crit"}>{ver.tests_passed ? "Tests passed" : "Tests failed"}</Badge>
          {ver.test_output && <pre className="scroll-x max-h-56 rounded-lg border border-line bg-page p-3 font-mono text-[11px] text-muted">{ver.test_output}</pre>}
        </Section>
      )}
      {!!matches?.length && (
        <Section title="Similar incidents (Atlas Vector Search)">
          <ul className="space-y-1 text-sm">{matches.map((m) => <li key={m.memory_id} className="font-mono text-xs text-muted">{m.memory_id} · score {m.score}</li>)}</ul>
        </Section>
      )}
      {prUrl && (
        <a href={prUrl} target="_blank" rel="noreferrer" className="btn">Open the draft PR <ArrowUpRight className="h-3.5 w-3.5" /></a>
      )}
    </section>
  );
}

export default function AgentWalkthrough() {
  const { incidentId } = useParams<{ incidentId: string }>();
  const { data, meta, error, updatedAt, reload } = usePoll<Walkthrough>(`/api/v1/incidents/${encodeURIComponent(incidentId)}/walkthrough`, 2500);
  const [picked, setPicked] = useState<string | null>(null);
  const current = data?.steps.find((s) => s.state === "running" || s.state === "failed" || s.state === "needs_review")
    ?? [...(data?.steps ?? [])].reverse().find((s) => s.state === "done");
  const step = data?.steps.find((s) => s.key === picked) ?? current;

  return (
    <Shell meta={meta} updatedAt={updatedAt} error={error}>
      <Link href="/ai-agent" className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted hover:text-ink">
        <ArrowLeft className="h-4 w-4" /> Investigations
      </Link>
      {!data && error ? <ErrorPanel error={error} onRetry={reload} />
        : !data ? <Skeleton className="h-64" />
        : (
          <>
            <PageHeader title="Investigation steps" sub={<>{data.title} · <Link href={`/incidents/${encodeURIComponent(data.incident_id)}`} className="font-mono text-xs hover:text-ink">{data.incident_id}</Link></>}>
              <div className="w-full sm:w-auto sm:text-right">
                <Badge tone={HEALTH[data.health.state].tone}>{HEALTH[data.health.state].label}</Badge>
                <p className="mt-1 max-w-sm text-xs text-muted">{data.health.reason}</p>
              </div>
            </PageHeader>
            <div className="space-y-6">
              <section className="panel p-3 sm:p-4" aria-label="Steps">
                <Pipeline steps={data.steps} selected={step?.key ?? ""} onSelect={setPicked} />
              </section>
              {step && <Detail step={step} />}
            </div>
          </>
        )}
    </Shell>
  );
}
