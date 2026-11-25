"use client";

import { SegmentedControl, SegmentedControlItem } from "@astryxdesign/core/SegmentedControl";
import { Button } from "@astryxdesign/core/Button";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, ChevronDown, GitBranch, Globe, HelpCircle, LayoutDashboard, LogOut, Menu, Plug, Search, Siren, XCircle } from "lucide-react";
import { type Account, type ApiError, type ErrorKind, type Meta, POLL_MS, type Status, type Window } from "@/lib/api";
import { STATUS_META, ago, clock, useNow } from "@/lib/format";
import packageInfo from "../../package.json";

export function StatusIcon({ status, className = "h-4 w-4" }: { status: Status; className?: string }) {
  const Icon = { operational: CheckCircle2, degraded: AlertTriangle, down: XCircle, unknown: HelpCircle }[status];
  return <Icon className={`flex-none ${className}`} style={{ color: STATUS_META[status].color }} aria-hidden="true" />;
}

export type Tone = "ok" | "warn" | "crit" | "accent" | "neutral";
export const STATUS_TONE: Record<Status, Tone> = { operational: "ok", degraded: "warn", down: "crit", unknown: "neutral" };

// Literal class names so Tailwind keeps them.
const TONE_CLASS: Record<Tone, string> = {
  ok: "tone-ok", warn: "tone-warn", crit: "tone-crit", accent: "tone-accent", neutral: "tone-neutral",
};

export function Badge({ tone, children, title }: { tone: Tone; children: React.ReactNode; title?: string }) {
  return <span title={title} className={`badge ${TONE_CLASS[tone]}`}>{children}</span>;
}

export function StatusPill({ status }: { status: Status }) {
  return (
    <Badge tone={STATUS_TONE[status]}>
      <StatusIcon status={status} className="h-3.5 w-3.5" />
      {STATUS_META[status].label}
    </Badge>
  );
}

const DOT: Record<Status, string> = { operational: "bg-ok", degraded: "bg-warn", down: "bg-crit", unknown: "bg-faint" };

/** A status dot that is never shown without its text label. */
export function StatusText({ status, label, className = "" }: { status: Status; label?: string; className?: string }) {
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap ${className}`}>
      <span className={`h-2 w-2 flex-none rounded-full ${DOT[status]}`} aria-hidden="true" />
      {label ?? STATUS_META[status].label}
    </span>
  );
}

/** Page title row: heading, optional subtitle and actions on the right. */
export function PageHeader({ title, sub, children }: { title: React.ReactNode; sub?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <div className="mb-7 flex flex-wrap items-end gap-4 border-b border-line pb-6">
      <div className="min-w-0 flex-1">
        <h1 className="page-title">{title}</h1>
        {sub && <p className="mt-3 max-w-2xl break-words text-sm leading-relaxed text-muted">{sub}</p>}
      </div>
      {children}
    </div>
  );
}

export function Logo() {
  return (
    <Link href="/" className="flex items-center gap-2.5" aria-label="OpsRelay home">
      <img src="/icon.svg" alt="" width="32" height="32" />
      <span className="text-xl font-semibold tracking-[-0.05em]">OpsRelay</span>
    </Link>
  );
}

/** Whether the data on screen is current: fresh, stale (refreshes missed) or unavailable (last refresh failed). */
export function freshness(updatedAt: number | null, error: boolean, now: number, intervalMs = POLL_MS) {
  if (updatedAt === null) return error ? "unavailable" : "loading";
  if (error) return "unavailable";
  return now - updatedAt > intervalMs * 2.5 ? "stale" : "fresh";
}

export function LiveIndicator({ updatedAt, error, intervalMs = POLL_MS }: { updatedAt: number | null; error: boolean; intervalMs?: number }) {
  const now = useNow();
  const state = freshness(updatedAt, error, now, intervalMs);
  const at = updatedAt ? new Date(updatedAt).toISOString() : null;
  const text = {
    loading: "Loading…",
    unavailable: at ? `Refresh failed · last refresh ${clock(at)}` : "Monitoring API unavailable",
    stale: `Stale · last refresh ${clock(at)}`,
    fresh: `Refreshed ${clock(at)} · every ${Math.round(intervalMs / 1000)}s`,
  }[state];
  return (
    <span className={`tabular inline-flex items-center gap-2 text-xs ${state === "fresh" || state === "loading" ? "text-muted" : "text-warn"}`} role="status">
      <span className={`h-2 w-2 flex-none rounded-full ${state === "fresh" ? "bg-ok" : state === "loading" ? "bg-faint" : "bg-warn"}`} aria-hidden="true" />
      {text}
    </span>
  );
}

/** Shown above the page when the visible data is the last known state rather than a current one. */
function StaleNotice({ updatedAt, error, intervalMs }: { updatedAt: number | null; error: ApiError | null; intervalMs: number }) {
  const now = useNow();
  const state = freshness(updatedAt, Boolean(error), now, intervalMs);
  if (!updatedAt || (state !== "stale" && state !== "unavailable")) return null;
  const at = new Date(updatedAt).toISOString();
  return (
    <p className="tabular mb-5 rounded-md border border-warn/30 bg-warn/[0.07] px-3 py-2 text-sm text-ink" role="alert">
      {state === "unavailable" && error ? error.message : "Refreshes are delayed: the last successful refresh is older than expected."} Showing the last known status from{" "}
      {clock(at)} ({ago(at, now)}). Retrying every {Math.round(intervalMs / 1000)}s.
    </p>
  );
}

export function WindowTabs({ value, onChange }: { value: Window; onChange: (w: Window) => void }) {
  const items: [Window, string][] = [["24h", "24 hours"], ["7d", "7 days"], ["30d", "30 days"]];
  return (
    <SegmentedControl label="Time window" size="md" value={value} onChange={(w) => onChange(w as Window)}>
      {items.map(([w, label]) => <SegmentedControlItem key={w} value={w} label={label} />)}
    </SegmentedControl>
  );
}

export function Stat({ label, value, sub, title }: { label: string; value: React.ReactNode; sub?: React.ReactNode; title?: string }) {
  return (
    <div className="min-w-0" title={title}>
      <p className="label">{label}</p>
      <p className="tabular mt-1.5 truncate text-[22px] font-semibold tracking-tight text-ink">{value}</p>
      {sub && <p className="mt-0.5 truncate text-xs text-muted">{sub}</p>}
    </div>
  );
}

const NAV = {
  Monitoring: [
    { href: "/overview", label: "Overview", Icon: LayoutDashboard },
    { href: "/incidents", label: "Incidents", Icon: Siren },
    { href: "/ai-agent", label: "Investigations", Icon: Search },
  ],
  Workspace: [
    { href: "/sites", label: "Sites", Icon: Globe },
    { href: "/repositories", label: "Repositories", Icon: GitBranch },
    { href: "/connectors", label: "Connectors", Icon: Plug },
  ],
};

function NavLinks() {
  const path = usePathname();
  return (
    <nav aria-label="Main" className="space-y-6">
      {Object.entries(NAV).map(([group, links]) => (
        <div key={group}>
          <p className="mb-2 px-3 text-xs font-medium text-faint">{group}</p>
          {links.map(({ href, label, Icon }) => {
            const active = href === "/overview" ? path === "/overview" || path.startsWith("/services") : path.startsWith(href);
            return (
              <Link key={href} href={href} aria-current={active ? "page" : undefined}
                className={`mb-1 flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm transition ${
                  active ? "bg-[#dce6d4] font-semibold text-[#21432f]" : "text-muted hover:bg-hover hover:text-ink"}`}>
                <Icon size={17} strokeWidth={1.7} aria-hidden="true" /> {label}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

export function Shell({ updatedAt, error, intervalMs = POLL_MS, children }: {
  meta: Meta | null; updatedAt: number | null; error: ApiError | null; intervalMs?: number; children: React.ReactNode;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  return (
    <div className="min-h-screen bg-page font-sans text-ink">
      <a href="#workspace" className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-3 focus:z-50 focus:bg-surface focus:p-2">Skip to content</a>
      <header className="sticky top-0 z-30 border-b border-line bg-surface">
        <div className="flex h-16 items-center gap-4 px-4 sm:px-7">
          <button type="button" className="rounded-md p-2 hover:bg-hover lg:hidden" aria-label="Navigation" aria-expanded={menuOpen}
            aria-controls="mobile-nav" onClick={() => setMenuOpen(!menuOpen)}><Menu size={20} /></button>
          <Logo />
          <div className="ml-auto flex min-w-0 items-center gap-4">
            <span className="hidden sm:block"><LiveIndicator updatedAt={updatedAt} error={Boolean(error)} intervalMs={intervalMs} /></span>
            <AccountMenu />
          </div>
        </div>
        <div id="mobile-nav" hidden={!menuOpen} className="max-h-[75dvh] overflow-auto border-t border-line p-4 lg:hidden">
          <NavLinks />
        </div>
      </header>
      <aside className="fixed inset-y-0 left-0 top-16 hidden w-52 flex-col border-r border-line bg-[#f0f1e9] px-3 pb-5 pt-7 lg:flex">
        <NavLinks />
        <p className="mx-3 mt-auto border-t border-line pt-4 font-mono text-[11px] text-faint">OpsRelay v{packageInfo.version}</p>
      </aside>
      <div className="lg:pl-52">
        <main id="workspace" className="mx-auto max-w-[1440px] px-4 pb-28 pt-5 sm:px-8 sm:pt-8 xl:px-12">
          <div className="mb-3 sm:hidden"><LiveIndicator updatedAt={updatedAt} error={Boolean(error)} intervalMs={intervalMs} /></div>
          <StaleNotice updatedAt={updatedAt} error={error} intervalMs={intervalMs} />
          {children}
        </main>
      </div>
    </div>
  );
}

function AccountMenu() {
  const [account, setAccount] = useState<Account | null>(null);
  useEffect(() => {
    fetch("/api/v1/auth/me", { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).then((b) => b && setAccount(b.data)).catch(() => undefined);
  }, []);
  if (!account) return null;
  return (
    <details className="group relative min-w-0 max-w-[36vw] text-xs sm:max-w-52">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-md px-1 py-1.5 text-muted hover:bg-raised/60 hover:text-ink [&::-webkit-details-marker]:hidden">
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-full border border-line-strong bg-raised font-semibold text-ink" aria-hidden="true">{account.user.email[0].toUpperCase()}</span>
        <span className="min-w-0 flex-1 truncate" title={account.user.email}>{account.user.email}</span>
        <ChevronDown className="h-3.5 w-3.5 flex-none transition group-open:rotate-180" aria-hidden="true" />
      </summary>
      <div className="absolute right-0 top-full mt-2 w-40 rounded-xl border border-line bg-surface p-3 shadow-lg">
        <button
          type="button" className="flex items-center gap-1.5 text-muted hover:text-ink"
          onClick={async () => {
            await fetch("/api/v1/auth/sign-out", { method: "POST" }).catch(() => undefined);
            window.location.assign("/sign-in");
          }}
        >
          <LogOut className="h-3.5 w-3.5" aria-hidden="true" /> Sign out
        </button>
      </div>
    </details>
  );
}

const ERROR_TITLE: Record<ErrorKind, string> = {
  dashboard_offline: "Dashboard server not responding",
  api_unreachable: "Uptime API not reachable",
  not_found: "Not found",
  rejected: "Request rejected",
  api_error: "Uptime API error",
  bad_response: "Unexpected response from the uptime API",
};

export function ErrorPanel({ error, onRetry }: { error: ApiError; onRetry?: () => void }) {
  return (
    <div className="panel mx-auto mt-16 max-w-md p-8 text-center" role="alert">
      <XCircle className="mx-auto h-8 w-8 text-crit" aria-hidden="true" />
      <p className="mt-4 font-semibold">{ERROR_TITLE[error.kind]}</p>
      <p className="mt-1 text-sm text-muted">{error.message}</p>
      {error.kind === "api_unreachable" && (
        <pre className="mt-5 rounded-lg bg-page p-3 text-left text-xs text-muted">cd admin-dashboard/backend{"\n"}.venv/bin/python -m uptime serve</pre>
      )}
      {onRetry && (
        <Button label="Retry" onClick={onRetry} className="mt-4" />
      )}
    </div>
  );
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-raised ${className}`} />;
}
