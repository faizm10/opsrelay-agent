"use client";

import { Button } from "@astryxdesign/core/Button";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import Link from "next/link";
import { GitBranch, Trash2 } from "lucide-react";
import { Combobox } from "@/components/combobox";
import { ErrorPanel, PageHeader, Shell, Skeleton } from "@/components/ui";
import { type AgentMode, send, usePoll } from "@/lib/api";

type Connection = { connected: boolean; login?: string };
type Mapping = { service_id: string; service_name: string; repo: string; base_branch: string; deployed_url: string; workflow: string | null; betterstack_monitor_id: string | null;
  dependabot?: { checked_at: string; result: string; reason?: string; open_alerts?: number } | null; sentry_project: string | null;
  actions?: { result: string; reason?: string; failing?: number } | null;
  backlog?: { result: string; reason?: string; open?: number } | null;
  vercel_project: string | null; vercel?: { result: string; reason?: string; state?: string | null; sha?: string | null } | null;
  agent: { enabled: boolean; mode: AgentMode; app: string | null } };
type AgentConfig = { worker_repo: string | null; apps: string[] };
type Repo = { full_name: string; default_branch: string; private: boolean };
type Options = { branches: string[]; workflows: { file: string; name: string }[] };

export default function Repositories() {
  const github = usePoll<Connection>("/api/v1/github", 60_000);
  const mapped = usePoll<Mapping[]>("/api/v1/repositories", 60_000);
  const agentConfig = usePoll<AgentConfig>("/api/v1/agent/config", 300_000);
  const [error, setError] = useState("");
  const remove = async (m: Mapping) => {
    try {
      await send("DELETE", `/api/v1/repositories/${encodeURIComponent(m.service_id)}`);
      toast.success(`${m.repo} removed`);
      mapped.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const loadError = github.error ?? mapped.error;
  return (
    <Shell meta={github.meta} updatedAt={github.updatedAt} error={loadError} intervalMs={60_000}>
      <PageHeader title="Repositories" sub="Each service maps to the GitHub repository, branch and deployment the repair agent works on." />
      {loadError && (!github.data || !mapped.data) ? <ErrorPanel error={loadError} onRetry={() => { github.reload(); mapped.reload(); }} />
        : !github.data || !mapped.data ? <Skeleton className="h-56" />
        : (
          <div className="space-y-6">
            {!github.data.connected && (
              <p className="panel p-5 text-sm text-muted">
                <Link href="/connectors" className="text-accent hover:underline">Connect GitHub</Link> first: only repositories its token can access can be mapped.
              </p>
            )}
            <section className="panel">
              <h2 className="border-b border-line px-5 py-3 section-title">Services</h2>
              {mapped.data.length === 0 ? (
                <p className="px-5 py-4 text-sm text-muted">No repositories mapped yet.</p>
              ) : (
                <ul className="divide-y divide-line">
                  {mapped.data.map((m) => (
                    <li key={m.service_id} className="flex flex-wrap items-center gap-x-6 gap-y-1 px-5 py-3 text-sm">
                      <span className="min-w-36 font-medium">{m.service_name}</span>
                      <span className="flex items-center gap-1.5 font-mono text-xs text-muted">
                        <GitBranch className="h-3.5 w-3.5" aria-hidden="true" />{m.repo} · {m.base_branch}
                      </span>
                      <a href={m.deployed_url} className="truncate text-xs text-accent hover:underline" target="_blank" rel="noreferrer">{m.deployed_url}</a>
                      <span className={`text-xs ${m.actions?.result === "skipped" ? "text-warn" : "text-faint"}`} title={m.actions?.reason}>
                        {`Actions (${m.workflow ?? "all workflows"}): `}
                        {!m.actions ? "not checked yet"
                          : m.actions.result === "skipped" ? "can't read runs (token needs Actions: read)"
                          : m.actions.failing ? `${m.actions.failing} failing` : "passing"}
                      </span>
                      <span className={`text-xs ${m.dependabot?.result === "skipped" ? "text-warn" : "text-faint"}`} title={m.dependabot?.reason}>
                        {!m.dependabot ? "Dependabot: not checked yet"
                          : m.dependabot.result === "skipped" ? "Dependabot: can't read alerts (token needs Dependabot alerts: read)"
                          : `Dependabot: ${m.dependabot.open_alerts} open alert${m.dependabot.open_alerts === 1 ? "" : "s"}`}
                      </span>
                      <span className={`text-xs ${m.backlog?.result === "skipped" ? "text-warn" : "text-faint"}`} title={m.backlog?.reason}>
                        {!m.backlog ? "PRs and issues: not checked yet"
                          : m.backlog.result === "skipped" ? "PRs and issues: can't read them (token needs Issues and Pull requests: read)"
                          : `PRs and issues: ${m.backlog.open} open`}
                      </span>
                      <MappingField mapping={m} label="Better Stack monitor" saved={m.betterstack_monitor_id} path="betterstack" field="monitor_id"
                        pattern="[0-9]*" placeholder="ID" onSaved={mapped.reload} />
                      <MappingField mapping={m} label="Sentry project" saved={m.sentry_project} path="sentry" field="project"
                        pattern="[A-Za-z0-9_\-]*" placeholder="slug" onSaved={mapped.reload} />
                      <MappingField mapping={m} label="Vercel project" saved={m.vercel_project} path="vercel" field="project"
                        pattern="[A-Za-z0-9._\-]*" placeholder="name" onSaved={mapped.reload} />
                      {m.vercel_project && (
                        <span className={`text-xs ${m.vercel?.state === "ERROR" || m.vercel?.result === "skipped" ? "text-warn" : "text-faint"}`} title={m.vercel?.reason}>
                          {!m.vercel ? "Vercel: not checked yet" : m.vercel.result === "skipped" ? `Vercel: ${m.vercel.reason}`
                            : `Vercel: ${m.vercel.state ?? "no deploys"}${m.vercel.sha ? ` · ${m.vercel.sha.slice(0, 7)}` : ""}`}
                        </span>
                      )}
                      {agentConfig.data && <AgentControl mapping={m} config={agentConfig.data} onSaved={mapped.reload} />}
                      <button type="button" className="btn ml-auto px-2" aria-label={`Remove ${m.service_name}`} onClick={() => remove(m)}>
                        <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {error && <p className="px-5 pb-3 text-sm text-crit" role="alert">{error}</p>}
            </section>
            {github.data.connected && <AddRepository onAdded={mapped.reload} />}
          </div>
        )}
    </Shell>
  );
}

function AddRepository({ onAdded }: { onAdded: () => void }) {
  const repos = usePoll<Repo[]>("/api/v1/github/repos", 300_000);
  const [repo, setRepo] = useState("");
  const [options, setOptions] = useState<Options | null>(null);
  const [form, setForm] = useState({ service_name: "", base_branch: "", deployed_url: "", workflow: "" });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });

  useEffect(() => {
    setOptions(null);
    if (!repo) return;
    const picked = repos.data?.find((r) => r.full_name === repo);
    setForm((f) => ({ ...f, base_branch: picked?.default_branch ?? "", workflow: "", service_name: f.service_name || repo.split("/")[1] }));
    let live = true;
    send<Options>("GET", `/api/v1/github/repos/${repo}`).then((o) => live && setOptions(o)).catch((e) => live && setError((e as Error).message));
    return () => { live = false; };
  }, [repo]); // not repos.data: a refetched list must not reset the form

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setPending(true);
    setError("");
    try {
      await send("POST", "/api/v1/repositories", { repo, ...form, workflow: form.workflow || null });
      toast.success(`${repo} added`);
      setRepo("");
      setForm({ service_name: "", base_branch: "", deployed_url: "", workflow: "" });
      onAdded();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPending(false);
    }
  };

  return (
    <form className="panel space-y-4 p-5" onSubmit={submit}>
      <h2 className="section-title">Add a repository</h2>
      {repos.error && <p className="text-sm text-crit" role="alert">{repos.error.message}</p>}
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="space-y-1.5">
          <label htmlFor="repo-picker" className="label">Repository</label>
          <Combobox id="repo-picker" value={repo} onChange={setRepo} disabled={!repos.data}
            placeholder={repos.data ? "Choose a repository" : "Loading repositories…"} searchPlaceholder="Search repositories…"
            empty="No repository matches." options={(repos.data ?? []).map((r) => ({ value: r.full_name, label: r.full_name, hint: r.private ? "private" : undefined }))} />
        </div>
        <label className="space-y-1.5">
          <span className="label">Service name</span>
          <input className="input" value={form.service_name} onChange={set("service_name")} required maxLength={60} />
        </label>
        <label className="space-y-1.5">
          <span className="label">Base branch</span>
          <select className="input" value={form.base_branch} onChange={set("base_branch")} required disabled={!options}>
            {(options?.branches ?? [form.base_branch]).map((b) => <option key={b} value={b}>{b}</option>)}
          </select>
        </label>
        <label className="space-y-1.5">
          <span className="label">Deployed URL</span>
          <input className="input" type="url" placeholder="https://" value={form.deployed_url} onChange={set("deployed_url")} required />
        </label>
        <label className="space-y-1.5 sm:col-span-2">
          <span className="label">Workflow check (optional)</span>
          <select className="input" value={form.workflow} onChange={set("workflow")} disabled={!options}>
            <option value="">None</option>
            {options?.workflows.map((w) => <option key={w.file} value={w.file}>{w.name} ({w.file})</option>)}
          </select>
        </label>
      </div>
      {error && <p className="text-sm text-crit" role="alert">{error}</p>}
      <Button type="submit" variant="primary" isLoading={pending} isDisabled={!options} label="Add repository" />
    </form>
  );
}

/** A per-service id for an alert source (Better Stack monitor, Sentry project), saved on its own. */
function MappingField({ mapping, label, saved, path, field, pattern, placeholder, onSaved }: {
  mapping: Mapping; label: string; saved: string | null; path: string; field: string; pattern: string; placeholder: string; onSaved: () => void;
}) {
  const [value, setValue] = useState(saved ?? "");
  const [error, setError] = useState("");
  const dirty = value !== (saved ?? "");
  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await send("PUT", `/api/v1/repositories/${encodeURIComponent(mapping.service_id)}/${path}`, { [field]: value || null });
      toast.success("Saved");
      setError("");
      onSaved();
    } catch (err) {
      setError((err as Error).message);
    }
  };
  return (
    <form onSubmit={save} className="flex items-center gap-1.5 text-xs">
      <label className="flex items-center gap-1.5">
        <span className="text-faint">{label}</span>
        <input className="input w-28 py-1 font-mono text-xs" pattern={pattern} placeholder={placeholder} value={value} onChange={(e) => setValue(e.target.value)} />
      </label>
      {dirty && <button type="submit" className="btn px-2">Save</button>}
      {error && <span className="text-crit" role="alert">{error}</span>}
    </form>
  );
}

/** The repair agent is off until switched on, and then only reports (no PR) unless draft PRs are chosen. */
function AgentControl({ mapping, config, onSaved }: { mapping: Mapping; config: AgentConfig; onSaved: () => void }) {
  // Repairs need the worker's own repository and apps.toml; any repository can be diagnosed.
  // Repair needs the shared worker's repository and at least one code area in agents/apps.toml.
  const repairable = config.worker_repo?.toLowerCase() === mapping.repo.toLowerCase() && config.apps.length > 0;
  const [mode, setMode] = useState<AgentMode>(repairable || mapping.agent.enabled ? mapping.agent.mode : "diagnose");
  const [app, setApp] = useState(mapping.agent.app ?? config.apps[0] ?? "");
  const [error, setError] = useState("");
  const save = async (enabled: boolean, next: { mode?: AgentMode; app?: string } = {}) => {
    try {
      await send("PUT", `/api/v1/repositories/${encodeURIComponent(mapping.service_id)}/agent`, { enabled, mode: next.mode ?? mode, app: next.app ?? app });
      toast.success(enabled ? `Agent on for ${mapping.repo}` : `Agent off for ${mapping.repo}`);
      setError("");
      onSaved();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const on = mapping.agent.enabled;
  return (
    <div className="flex w-full flex-wrap items-center gap-2 text-xs">
      <button
        type="button" role="switch" aria-checked={on} onClick={() => save(!on)}
        className={`relative h-5 w-9 flex-none rounded-full border transition ${on ? "border-accent bg-accent" : "border-line-strong bg-raised"}`}
      >
        <span className={`absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white transition-all ${on ? "left-[18px]" : "left-0.5"}`} />
        <span className="sr-only">Agent for {mapping.service_name}</span>
      </button>
      <span className="text-muted">Agent {on ? "on" : "off"}</span>
      <label className="flex items-center gap-1.5">
        <span className="text-faint">Mode</span>
        <select className="input w-auto py-1 text-xs" value={mode}
          onChange={(e) => { const m = e.target.value as AgentMode; setMode(m); if (on) save(true, { mode: m }); }}>
          <option value="diagnose">Diagnose failures</option>
          {repairable && <option value="report_only">Repair, report only</option>}
          {repairable && <option value="draft_pr">Repair, open draft PRs</option>}
        </select>
      </label>
      {mode === "diagnose" ? (
        <span className="text-faint">Reads failed CI logs and recent commits (sent to OpenAI) and explains the likely cause. Never runs your code.</span>
      ) : <label className="flex items-center gap-1.5">
        <span className="text-faint">Code area</span>
        <select className="input w-auto py-1 text-xs" value={app}
          onChange={(e) => { setApp(e.target.value); if (on) save(true, { app: e.target.value }); }}>
          {config.apps.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
      </label>}
      {error && <span className="basis-full text-crit" role="alert">{error}</span>}
    </div>
  );
}
