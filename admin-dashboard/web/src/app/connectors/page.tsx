"use client";

import { Button } from "@astryxdesign/core/Button";
import { useState } from "react";
import { toast } from "sonner";
import { ErrorPanel, PageHeader, Shell, Skeleton } from "@/components/ui";
import { send, usePoll } from "@/lib/api";

type Connection = { connected: boolean; login?: string; connected_at?: string };
type VercelConnection = { connected: boolean; login?: string; team_id?: string | null };
type OpenAIKey = { configured: boolean; hint?: string; saved_at?: string };
type Sentry = { configured: boolean; webhook_path?: string; has_secret?: boolean;
  last_delivery?: { at: string; result: string; reason?: string; incident_id?: string } | null };
type Notifier = { kind: string; label: string; configured: boolean; target?: string;
  last_delivery?: { at: string; event: string; error?: string | null } | null };
type BetterStack = { configured: boolean; webhook_path?: string; secret_header?: string; secret?: string;
  last_delivery?: { at: string; result: string; reason?: string; incident_id?: string } | null };

export default function Connectors() {
  const github = usePoll<Connection>("/api/v1/github", 60_000);
  return (
    <Shell meta={github.meta} updatedAt={github.updatedAt} error={github.error} intervalMs={60_000}>
      <PageHeader title="Connectors" sub="Where OpsRelay reads code and alerts from. Secrets stay on the server and are shown only once." />
      {github.error && !github.data ? <ErrorPanel error={github.error} onRetry={github.reload} /> : !github.data ? <Skeleton className="h-56" /> : (
        <div className="grid items-start gap-5 xl:grid-cols-2">
          <GitHubPanel connection={github.data} onChange={github.reload} />
          <BetterStackPanel />
          <SentryPanel />
          <VercelPanel />
          <OpenAIPanel />
          <NotificationsPanel />
        </div>
      )}
    </Shell>
  );
}

function GitHubPanel({ connection, onChange }: { connection: Connection; onChange: () => void }) {
  const [token, setToken] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>, done: string) => {
    setPending(true);
    setError("");
    try {
      await action();
      toast.success(done);
      setToken("");
      onChange();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  };

  return (
    <section className="panel p-5">
      <h2 className="section-title">GitHub</h2>
      {connection.connected ? (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
          <span className="badge tone-ok">Connected as {connection.login}</span>
          <button type="button" className="btn" disabled={pending} onClick={() => run(() => send("DELETE", "/api/v1/github"), "GitHub disconnected")}>Disconnect</button>
        </div>
      ) : (
        <form className="mt-2 space-y-3" onSubmit={(e) => { e.preventDefault(); run(() => send("PUT", "/api/v1/github", { token }), "GitHub connected"); }}>
          <p className="text-sm text-muted">
            Paste a GitHub token. Only the repositories it can access can be mapped. A{" "}
            <a className="text-accent hover:underline" href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noreferrer">fine-grained token</a>{" "}
            with Contents, Pull requests, Actions and Dependabot alerts (read) on the chosen repositories is enough. It stays on the server.
          </p>
          <div className="flex flex-wrap gap-2">
            <label className="min-w-0 flex-1">
              <span className="sr-only">GitHub token</span>
              <input className="input font-mono" type="password" autoComplete="off" placeholder="github_pat_…" value={token} onChange={(e) => setToken(e.target.value)} required minLength={20} />
            </label>
            <Button type="submit" variant="primary" isLoading={pending} label="Connect GitHub" />
          </div>
        </form>
      )}
      {error && <p className="mt-2 text-sm text-crit" role="alert">{error}</p>}
    </section>
  );
}

function BetterStackPanel() {
  const conf = usePoll<BetterStack>("/api/v1/betterstack", 60_000);
  const [created, setCreated] = useState<BetterStack | null>(null);
  const [error, setError] = useState("");
  const shown = created ?? conf.data;
  const rotate = async () => {
    if (shown?.configured && !window.confirm("Replace the webhook URL and secret? Better Stack must be updated with the new ones.")) return;
    try {
      setCreated(await send<BetterStack>("POST", "/api/v1/betterstack"));
      toast.success("Better Stack webhook URL created");
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  };
  if (!shown) return null;
  const last = shown.last_delivery;
  return (
    <section className="panel space-y-3 p-5">
      <h2 className="section-title">Better Stack alerts</h2>
      {!shown.configured ? (
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-muted">Get a webhook URL, then map each service to its Better Stack monitor on the Repositories page.</p>
          <button type="button" className="btn btn-primary" onClick={rotate}>Create webhook</button>
        </div>
      ) : (
        <>
          <p className="text-sm text-muted">
            In Better Stack, add an outgoing webhook integration with this URL and a custom header. Keep <code>relationships</code> in the
            body template so OpsRelay can tell which monitor fired.
          </p>
          <dl className="grid gap-2 text-xs sm:grid-cols-[9rem_1fr]">
            <dt className="label">Webhook URL</dt>
            <dd className="break-all font-mono">{window.location.origin}{shown.webhook_path}</dd>
            <dt className="label">Header</dt>
            <dd className="break-all font-mono">{shown.secret_header}: {shown.secret ?? "(shown once when created; rotate to get a new one)"}</dd>
            <dt className="label">Last delivery</dt>
            <dd>{last ? `${new Date(last.at).toLocaleString()} · ${last.result}${last.reason ? ` · ${last.reason}` : ""}` : "None yet"}</dd>
          </dl>
          {shown.secret && <p className="text-xs text-warn">Copy the secret now. It isn&apos;t shown again.</p>}
          <button type="button" className="btn" onClick={rotate}>Rotate secret</button>
        </>
      )}
      {error && <p className="text-sm text-crit" role="alert">{error}</p>}
    </section>
  );
}

// What each outgoing connector asks for. The value is a secret: after saving only its last characters are shown.
const NOTIFIER_FORM: Record<string, { field: string; placeholder: string; help: string; href: string }> = {
  slack: { field: "url", placeholder: "https://hooks.slack.com/services/…", help: "Create an incoming webhook for the channel",
    href: "https://api.slack.com/messaging/webhooks" },
  discord: { field: "url", placeholder: "https://discord.com/api/webhooks/…", help: "Channel settings → Integrations → Webhooks",
    href: "https://support.discord.com/hc/en-us/articles/228383668" },
  webhook: { field: "url", placeholder: "https://your-service.example/opsrelay", help: "Any endpoint (Zapier, n8n, your API) that verifies X-OpsRelay-Signature",
    href: "https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries" },
  pagerduty: { field: "routing_key", placeholder: "32-character integration key", help: "Add an Events API v2 integration to a PagerDuty service",
    href: "https://support.pagerduty.com/main/docs/services-and-integrations" },
};

function NotificationsPanel() {
  const list = usePoll<Notifier[]>("/api/v1/notifications", 60_000);
  if (!list.data) return null;
  return (
    <section className="panel xl:col-span-2">
      <div className="border-b border-line px-5 py-3">
        <h2 className="section-title">Notifications</h2>
        <p className="mt-0.5 text-sm text-muted">
          Posts when an incident opens or resolves, when the repair agent opens a draft PR, and when it needs a human. Sent with the 5-minute check. PagerDuty only gets opens and resolves.
        </p>
      </div>
      <ul className="divide-y divide-line">
        {list.data.map((n) => <NotifierRow key={n.kind} notifier={n} onChange={list.reload} />)}
      </ul>
    </section>
  );
}

function NotifierRow({ notifier: n, onChange }: { notifier: Notifier; onChange: () => void }) {
  const form = NOTIFIER_FORM[n.kind];
  const [value, setValue] = useState("");
  const [pending, setPending] = useState(false);
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>, done: string) => {
    setPending(true);
    setError("");
    try {
      await action();
      setValue("");
      toast.success(done);
      onChange();
    } catch (e) {
      setError((e as Error).message);
      onChange();
    } finally {
      setPending(false);
    }
  };
  const last = n.last_delivery;
  return (
    <li className="space-y-2 px-5 py-4 text-sm">
      <div className="flex flex-wrap items-center gap-3">
        <span className="min-w-20 font-medium">{n.label}</span>
        {n.configured ? (
          <>
            <span className="badge tone-ok">Connected <span className="font-mono">{n.target}</span></span>
            <button type="button" className="btn" disabled={pending} onClick={() => run(() => send("POST", `/api/v1/notifications/${n.kind}/test`), "Test message sent")}>Send test</button>
            <button type="button" className="btn" disabled={pending} onClick={() => run(() => send("DELETE", `/api/v1/notifications/${n.kind}`), `${n.label} removed`)}>Remove</button>
          </>
        ) : form && (
          <form className="flex min-w-0 flex-1 flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); run(async () => setSecret((await send<{ secret?: string }>("PUT", `/api/v1/notifications/${n.kind}`, { [form.field]: value })).secret ?? ""), `${n.label} saved`); }}>
            <label className="min-w-0 flex-1">
              <span className="sr-only">{n.label} {form.field}</span>
              <input className="input font-mono" type="password" autoComplete="off" placeholder={form.placeholder} value={value} onChange={(e) => setValue(e.target.value)} required />
            </label>
            <Button type="submit" variant="primary" isLoading={pending} label="Save" />
          </form>
        )}
      </div>
      {!n.configured && form && <p className="text-xs text-muted"><a className="text-accent hover:underline" href={form.href} target="_blank" rel="noreferrer">{form.help}</a>, then paste it here.</p>}
      {n.configured && (
        <p className={`text-xs ${last?.error ? "text-crit" : "text-faint"}`}>
          {last ? `Last delivery ${new Date(last.at).toLocaleString()} · ${last.event}${last.error ? ` · ${last.error}` : " · delivered"}` : "Nothing sent yet."}
        </p>
      )}
      {secret && (
        <p className="break-all text-xs text-warn">
          Signing secret (copy it now, it isn&apos;t shown again): <span className="font-mono">{secret}</span>. Each request carries
          <code> X-OpsRelay-Signature: sha256=</code>HMAC-SHA256 of the raw body with this secret.
        </p>
      )}
      {error && <p className="text-xs text-crit" role="alert">{error}</p>}
    </li>
  );
}

function SentryPanel() {
  const conf = usePoll<Sentry>("/api/v1/sentry", 60_000);
  const [secret, setSecret] = useState("");
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>, done: string) => {
    try {
      await action();
      toast.success(done);
      setSecret("");
      setError("");
      conf.reload();
    } catch (e) {
      setError((e as Error).message);
    }
  };
  if (!conf.data) return null;
  const c = conf.data;
  const last = c.last_delivery;
  return (
    <section className="panel space-y-3 p-5">
      <h2 className="section-title">Sentry issues</h2>
      {!c.configured ? (
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-muted">Unresolved Sentry issues open an incident on the mapped service, and close it once they&apos;re resolved or ignored.</p>
          <button type="button" className="btn btn-primary" onClick={() => run(() => send("POST", "/api/v1/sentry"), "Sentry webhook URL created")}>Create webhook URL</button>
        </div>
      ) : (
        <>
          <p className="text-sm text-muted">
            In Sentry, go to Settings → Developer Settings → Custom Integrations and create an internal integration with this webhook URL. Turn
            on <code>issue</code> webhooks, then paste its Client Secret here. Set each service&apos;s Sentry project slug on the Repositories page.
          </p>
          <dl className="grid gap-2 text-xs sm:grid-cols-[9rem_1fr]">
            <dt className="label">Webhook URL</dt>
            <dd className="break-all font-mono">{window.location.origin}{c.webhook_path}</dd>
            <dt className="label">Client secret</dt>
            <dd>{c.has_secret ? "Saved" : "Not saved yet: deliveries are rejected until it is"}</dd>
            <dt className="label">Last delivery</dt>
            <dd>{last ? `${new Date(last.at).toLocaleString()} · ${last.result}${last.reason ? ` · ${last.reason}` : ""}` : "None yet"}</dd>
          </dl>
          <form className="flex flex-wrap gap-2" onSubmit={(e) => { e.preventDefault(); run(() => send("PUT", "/api/v1/sentry/secret", { client_secret: secret }), "Sentry client secret saved"); }}>
            <label className="min-w-0 flex-1">
              <span className="sr-only">Sentry client secret</span>
              <input className="input font-mono" type="password" autoComplete="off" placeholder={c.has_secret ? "Replace the client secret" : "Client secret"}
                value={secret} onChange={(e) => setSecret(e.target.value)} required minLength={32} />
            </label>
            <button type="submit" className="btn">Save secret</button>
          </form>
        </>
      )}
      {error && <p className="text-sm text-crit" role="alert">{error}</p>}
    </section>
  );
}

function VercelPanel() {
  const conn = usePoll<VercelConnection>("/api/v1/vercel", 60_000);
  const [form, setForm] = useState({ token: "", team_id: "" });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>, done: string) => {
    setPending(true);
    setError("");
    try {
      await action();
      toast.success(done);
      setForm({ token: "", team_id: "" });
      conn.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  };
  if (!conn.data) return null;
  return (
    <section className="panel p-5">
      <h2 className="section-title">Vercel deployments</h2>
      {conn.data.connected ? (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
          <span className="badge tone-ok">Connected as {conn.data.login}{conn.data.team_id ? ` · team ${conn.data.team_id}` : ""}</span>
          <button type="button" className="btn" disabled={pending} onClick={() => run(() => send("DELETE", "/api/v1/vercel"), "Vercel disconnected")}>Disconnect</button>
          <p className="basis-full text-xs text-muted">Set each service&apos;s Vercel project on the Repositories page. A failed production deploy opens an incident until the next one is ready.</p>
        </div>
      ) : (
        <form className="mt-2 space-y-3" onSubmit={(e) => { e.preventDefault(); run(() => send("PUT", "/api/v1/vercel", { token: form.token, team_id: form.team_id || null }), "Vercel connected"); }}>
          <p className="text-sm text-muted">
            Paste a <a className="text-accent hover:underline" href="https://vercel.com/account/settings/tokens" target="_blank" rel="noreferrer">Vercel token</a>.
            For projects owned by a team, add the team ID (Team Settings → General). It stays on the server.
          </p>
          <div className="flex flex-wrap gap-2">
            <label className="min-w-0 flex-1">
              <span className="sr-only">Vercel token</span>
              <input className="input font-mono" type="password" autoComplete="off" placeholder="Vercel token" value={form.token}
                onChange={(e) => setForm({ ...form, token: e.target.value })} required minLength={20} />
            </label>
            <label className="w-48">
              <span className="sr-only">Team ID (optional)</span>
              <input className="input font-mono" placeholder="team_… (optional)" value={form.team_id} onChange={(e) => setForm({ ...form, team_id: e.target.value })} />
            </label>
            <Button type="submit" variant="primary" isLoading={pending} label="Connect Vercel" />
          </div>
        </form>
      )}
      {error && <p className="mt-2 text-sm text-crit" role="alert">{error}</p>}
    </section>
  );
}

function OpenAIPanel() {
  const conn = usePoll<OpenAIKey>("/api/v1/openai", 60_000);
  const [key, setKey] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const run = async (action: () => Promise<unknown>, done: string) => {
    setPending(true);
    setError("");
    try {
      await action();
      toast.success(done);
      setKey("");
      conn.reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setPending(false);
    }
  };
  if (!conn.data) return null;
  return (
    <section className="panel p-5">
      <h2 className="section-title">OpenAI API key <span className="font-normal text-muted">(optional)</span></h2>
      {conn.data.configured ? (
        <div className="mt-2 flex flex-wrap items-center gap-3 text-sm">
          <span className="badge tone-ok">Your key · ends in {conn.data.hint}</span>
          <button type="button" className="btn" disabled={pending} onClick={() => run(() => send("DELETE", "/api/v1/openai"), "OpenAI key removed. The shared key is used again.")}>Remove</button>
          <p className="basis-full text-xs text-muted">The assistant and diagnose mode use this key and bill your OpenAI account, with no daily diagnosis limit.</p>
        </div>
      ) : (
        <form className="mt-2 space-y-3" onSubmit={(e) => { e.preventDefault(); run(() => send("PUT", "/api/v1/openai", { key }), "OpenAI key saved"); }}>
          <p className="text-sm text-muted">
            The assistant and diagnose mode use OpsRelay&apos;s shared key, limited to 10 diagnoses a day. Add your own{" "}
            <a className="text-accent hover:underline" href="https://platform.openai.com/api-keys" target="_blank" rel="noreferrer">OpenAI key</a>{" "}
            to use your account instead. It is encrypted and stays on the server.
          </p>
          <div className="flex flex-wrap gap-2">
            <label className="min-w-0 flex-1">
              <span className="sr-only">OpenAI API key</span>
              <input className="input font-mono" type="password" autoComplete="off" placeholder="sk-…" value={key}
                onChange={(e) => setKey(e.target.value)} required minLength={20} />
            </label>
            <Button type="submit" variant="primary" isLoading={pending} label="Save key" />
          </div>
        </form>
      )}
      {error && <p className="mt-2 text-sm text-crit" role="alert">{error}</p>}
    </section>
  );
}
