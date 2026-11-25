"use client";

import Link from "next/link";
import { type Incident, isDemoIncident } from "@/lib/api";
import { FAILURE_LABELS, clock, duration, useNow } from "@/lib/format";
import { phase } from "@/lib/phase";
import { Badge } from "./ui";

/** One incident as a keyboard-focusable row: what broke, its phase, and how long it has run. */
export function IncidentRow({ incident }: { incident: Incident }) {
  const now = useNow();
  const open = incident.state === "open";
  const live = open ? (now - new Date(incident.started_at).getTime()) / 1000 : incident.duration_seconds;
  const p = phase(incident);
  const demo = isDemoIncident(incident);
  return (
    <li>
      <Link href={`/incidents/${encodeURIComponent(incident.id)}`}
        className="block px-4 py-3 transition hover:bg-hover focus-visible:bg-hover sm:px-5 md:grid md:grid-cols-[minmax(0,1fr)_auto_9rem] md:items-center md:gap-6">
        <div className="min-w-0">
          <p className="break-words text-sm font-medium md:truncate text-ink">{incident.title.startsWith(incident.service_name) ? incident.title : `${incident.service_name} · ${incident.title}`}</p>
          <p className="truncate text-xs text-muted">
            {incident.evidence.source === "betterstack" ? "Better Stack alert"
              : incident.evidence.source === "dependabot" ? "Dependabot security alerts"
              : incident.evidence.source === "sentry" ? "Sentry issues"
              : incident.evidence.source === "vercel" ? "Vercel deploy failed"
              : incident.evidence.source === "actions" ? "GitHub Actions failing"
              : <>{demo ? `Service ${open ? incident.severity : "recovered"}` : FAILURE_LABELS[incident.failure_type ?? ""] ?? incident.failure_type}
                {" · "}{incident.failed_check_count} failed check{incident.failed_check_count === 1 ? "" : "s"}</>}
          </p>
        </div>
        <div className="mt-1.5 md:mt-0"><Badge tone={p.tone}>{p.label}</Badge></div>
        <p className="tabular mt-1.5 text-xs text-muted md:mt-0 md:text-right">
          {clock(incident.started_at, true)}
          <span className={`block ${open ? "text-ink" : "text-faint"}`}>{open ? `ongoing ${duration(live)}` : `lasted ${duration(live)}`}</span>
        </p>
      </Link>
    </li>
  );
}

export function IncidentList({ title, incidents, empty }: { title: string; incidents: Incident[]; empty: string }) {
  const id = `${title.toLowerCase().replace(/\W+/g, "-")}-title`;
  return (
    <section aria-labelledby={id}>
      <h2 id={id} className="section-title mb-3">
        {title}{incidents.length > 0 && <span className="tabular ml-1.5 font-normal text-muted">{incidents.length}</span>}
      </h2>
      {incidents.length === 0 ? (
        <p className="panel px-4 py-3 text-sm text-muted sm:px-5">{empty}</p>
      ) : (
        <ul className="panel divide-y divide-line overflow-hidden">{incidents.map((i) => <IncidentRow key={i.id} incident={i} />)}</ul>
      )}
    </section>
  );
}
