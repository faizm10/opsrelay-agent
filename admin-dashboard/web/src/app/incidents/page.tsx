"use client";

import { IncidentList } from "@/components/incidents";
import { ErrorPanel, PageHeader, Shell, Skeleton } from "@/components/ui";
import { type Incident, usePoll } from "@/lib/api";

export default function Incidents() {
  const active = usePoll<Incident[]>("/api/v1/incidents?state=open&limit=100");
  const resolved = usePoll<Incident[]>("/api/v1/incidents?state=resolved&limit=100");
  const groups = [
    { title: "Active", result: active, empty: "No active incidents." },
    { title: "Resolved", result: resolved, empty: "No resolved incidents yet." },
  ];
  return (
    <Shell meta={active.meta ?? resolved.meta} updatedAt={active.updatedAt ?? resolved.updatedAt} error={active.error ?? resolved.error}>
      <PageHeader title="Incidents" sub="The latest 100 incidents in each group, newest first." />
      <div className="space-y-8">
        {groups.map(({ title, result, empty }) => result.data
          ? <IncidentList key={title} title={title} incidents={result.data} empty={empty} />
          : <section key={title}>
              <h2 className="mb-2 text-sm font-semibold">{title}</h2>
              {result.error ? <ErrorPanel error={result.error} onRetry={result.reload} /> : <Skeleton className="h-72" />}
            </section>)}
      </div>
    </Shell>
  );
}
