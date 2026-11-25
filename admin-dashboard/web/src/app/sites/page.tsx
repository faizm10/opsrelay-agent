"use client";

import { Button } from "@astryxdesign/core/Button";
import { useState } from "react";
import { toast } from "sonner";
import { Trash2 } from "lucide-react";
import { ErrorPanel, PageHeader, Shell, Skeleton } from "@/components/ui";
import { send, usePoll } from "@/lib/api";

type Site = { id: string; name: string; url: string; health_url: string | null };

export default function Sites() {
  const sites = usePoll<Site[]>("/api/v1/sites", 60_000);
  const [error, setError] = useState("");
  const remove = async (site: Site) => {
    if (!window.confirm(`Stop monitoring ${site.name}?`)) return;
    try {
      await send("DELETE", `/api/v1/sites/${encodeURIComponent(site.id)}`);
      toast.success(`Stopped monitoring ${site.name}`);
      sites.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <Shell meta={sites.meta} updatedAt={sites.updatedAt} error={sites.error} intervalMs={60_000}>
      <PageHeader title="Sites" sub="The websites you monitor. Each one is checked every 5 minutes." />
      {sites.error && !sites.data ? <ErrorPanel error={sites.error} onRetry={sites.reload} />
        : !sites.data ? <Skeleton className="h-40" />
        : (
          <div className="space-y-6">
            <section className="panel">
              <h2 className="border-b border-line px-5 py-3 section-title">Monitored sites</h2>
              {sites.data.length === 0 ? (
                <p className="px-5 py-4 text-sm text-muted">No sites yet. Add one below.</p>
              ) : (
                <ul className="divide-y divide-line">
                  {sites.data.map((s) => (
                    <li key={s.id} className="flex flex-wrap items-center gap-x-6 gap-y-1 px-5 py-3 text-sm">
                      <span className="min-w-36 font-medium">{s.name}</span>
                      <a href={s.url} className="truncate text-xs text-accent hover:underline" target="_blank" rel="noreferrer">{s.url}</a>
                      <span className="truncate text-xs text-faint">{s.health_url ? `Health: ${s.health_url}` : "No health check"}</span>
                      <button type="button" className="btn ml-auto px-2" aria-label={`Remove ${s.name}`} onClick={() => remove(s)}>
                        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {error && <p className="px-5 pb-3 text-sm text-crit" role="alert">{error}</p>}
            </section>
            <AddSite onAdded={sites.reload} />
          </div>
        )}
    </Shell>
  );
}

function AddSite({ onAdded }: { onAdded: () => void }) {
  const [form, setForm] = useState({ name: "", url: "", health_url: "" });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [key]: e.target.value });

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setPending(true);
    setError("");
    try {
      await send("POST", "/api/v1/sites", { name: form.name, url: form.url, health_url: form.health_url || null });
      toast.success(`Monitoring ${form.name}`);
      setForm({ name: "", url: "", health_url: "" });
      onAdded();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPending(false);
    }
  }

  return (
    <form onSubmit={submit} className="panel space-y-3 p-5">
      <h2 className="section-title">Add a site</h2>
      <p className="text-xs text-muted">
        OpsRelay loads the page URL. If you add a health URL, it must answer 2xx, and a JSON <code>status</code> field, if
        present, must be ok/healthy. The first check runs right away.
      </p>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className="space-y-1 text-xs"><span className="label">Name</span>
          <input className="input w-full" required maxLength={80} value={form.name} onChange={set("name")} placeholder="Storefront" />
        </label>
        <label className="space-y-1 text-xs"><span className="label">Page URL</span>
          <input className="input w-full" required type="url" value={form.url} onChange={set("url")} placeholder="https://example.com" />
        </label>
        <label className="space-y-1 text-xs"><span className="label">Health URL (optional)</span>
          <input className="input w-full" type="url" value={form.health_url} onChange={set("health_url")} placeholder="https://api.example.com/health" />
        </label>
      </div>
      <Button type="submit" variant="primary" isLoading={pending} label={pending ? "Adding and checking…" : "Add site"} />
      {error && <p className="text-sm text-crit" role="alert">{error}</p>}
    </form>
  );
}
