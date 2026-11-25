"use client";

import { useEffect, useState } from "react";
import type { Status } from "./api";

export const STATUS_META: Record<Status, { label: string; color: string; text: string; soft: string }> = {
  operational: { label: "Operational", color: "#297346", text: "text-ok", soft: "bg-ok/10 border-ok/25" },
  degraded: { label: "Degraded", color: "#92600d", text: "text-warn", soft: "bg-warn/10 border-warn/25" },
  down: { label: "Down", color: "#b83c37", text: "text-crit", soft: "bg-crit/10 border-crit/30" },
  unknown: { label: "Unknown", color: "#646a76", text: "text-muted", soft: "bg-raised border-line-strong" },
};

export const FAILURE_LABELS: Record<string, string> = {
  backend_unreachable: "Backend unreachable",
  backend_timeout: "Backend timeout",
  availability_http_error: "Availability check error",
  frontend_unreachable: "Website unreachable",
  frontend_timeout: "Website timeout",
  frontend_http_error: "Website error",
  functional_probe_failed: "Functional probe failed",
  functional_probe_timeout: "Functional probe timeout",
  functional_probe_error: "Functional probe error",
  monitor_error: "Monitor error",
};

export const ROUTE_LABELS = {
  service_health: { label: "Service health", hint: "Unreachable: check process, deploy, network. Not a code repair." },
  investigate: { label: "Investigate logic", hint: "Reachable but a business rule fails. Candidate for AI diagnosis." },
};

/** Re-render every `ms` so relative times stay live. */
export function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(t);
  }, [ms]);
  return now;
}

export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function ago(iso: string | null | undefined, now: number): string {
  if (!iso) return "never";
  const s = (now - new Date(iso).getTime()) / 1000;
  if (s < 5) return "just now";
  return `${duration(s)} ago`;
}

export function clock(iso: string | null | undefined, withDate = false): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return withDate
    ? d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" })
    : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}

export function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  if (value === 100) return "100%";
  return `${value.toFixed(digits)}%`;
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value < 10 ? `${value.toFixed(1)} ms` : `${Math.round(value)} ms`;
}
