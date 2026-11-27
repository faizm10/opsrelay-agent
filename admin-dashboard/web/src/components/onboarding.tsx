"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Check, X } from "lucide-react";
import type { Summary } from "@/lib/api";
import { setupSteps } from "@/lib/setup";

const DISMISSED_KEY = "opsrelay-onboarding-dismissed";

/** First-run checklist. Hidden once every step is done, or after the viewer dismisses it for this workspace. */
export function Onboarding({ summary }: { summary: Summary }) {
  const ws = summary.setup.workspace_id;
  // Start hidden so a dismissed checklist doesn't flash before localStorage is read.
  const [dismissed, setDismissed] = useState(true);
  useEffect(() => setDismissed(window.localStorage.getItem(`${DISMISSED_KEY}:${ws}`) === "1"), [ws]);

  const steps = setupSteps(summary);
  const done = steps.filter((s) => s.done).length;
  if (dismissed || done === steps.length) return null;
  const next = steps.find((s) => !s.done);
  const dismiss = () => {
    window.localStorage.setItem(`${DISMISSED_KEY}:${ws}`, "1");
    setDismissed(true);
  };

  return (
    <section aria-labelledby="onboarding-title" className="panel p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 id="onboarding-title" className="text-base font-semibold">{done === 0 ? "Welcome to OpsRelay" : "Finish setting up"}</h2>
          <p className="mt-0.5 text-sm text-muted">
            {done === 0 ? "A few steps and OpsRelay will watch your sites, open incidents and point at the code behind them."
              : `${done} of ${steps.length} steps done.`}
          </p>
        </div>
        <button type="button" onClick={dismiss} className="rounded-sm p-1 text-faint hover:text-ink" aria-label="Hide setup checklist" title="Hide">
          <X className="h-4 w-4" aria-hidden="true" />
        </button>
      </div>
      <div className="mt-4 h-1 overflow-hidden rounded-full bg-line" role="progressbar" aria-label="Setup progress"
        aria-valuemin={0} aria-valuemax={steps.length} aria-valuenow={done}>
        <div className="h-full rounded-full bg-accent transition-all" style={{ width: `${(done / steps.length) * 100}%` }} />
      </div>
      <ol className="mt-3 divide-y divide-line">
        {steps.map((s, i) => (
          <li key={s.key} className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3 text-sm">
            <span className={`flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs ${
              s.done ? "border-ok/40 bg-ok/15 text-ok" : s === next ? "border-accent text-accent" : "border-line text-faint"}`}>
              {s.done ? <Check className="h-3.5 w-3.5" aria-label="Done" /> : i + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span className={s.done ? "text-muted" : "font-medium text-ink"}>{s.title}</span>
              {!s.done && <span className="block text-xs text-muted">{s.detail}</span>}
            </span>
            {!s.done && <Link href={s.href} className={`btn ${s === next ? "btn-primary" : ""}`}>{s.label}</Link>}
          </li>
        ))}
      </ol>
    </section>
  );
}
