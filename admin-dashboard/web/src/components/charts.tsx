"use client";

import { useMemo, useState } from "react";
import type { Bar, Status, Window } from "@/lib/api";
import { STATUS_META, clock, duration, ms } from "@/lib/format";
import { StatusIcon } from "./ui";

const WINDOW_START_LABEL: Record<Window, string> = { "24h": "24 hours ago", "7d": "7 days ago", "30d": "30 days ago" };

function barColor(status: Status) {
  return status === "unknown" ? "#c4c9bd" : STATUS_META[status].color;
}

/* ----------------------------------------------------------- uptime bars */
function describe(b: Bar, window: Window): string {
  const when = `${clock(b.start, true)} to ${clock(b.end, window !== "24h")}`;
  if (b.status === "unknown") return `${when}: no data recorded`;
  return `${when}: ${STATUS_META[b.status].label}, ${b.uptime_pct?.toFixed(2)}% uptime, ${b.checks} checks${b.failed_checks ? `, ${b.failed_checks} failed` : ""}`;
}

export function UptimeBars({ bars, window, height = 34, label, compact = false }: {
  bars: Bar[]; window: Window; height?: number; label: string; compact?: boolean;
}) {
  const [active, setActive] = useState<number | null>(null);
  const hovered = active !== null ? bars[active] : null;
  const leftPct = active !== null ? ((active + 0.5) / bars.length) * 100 : 0;
  const counts = useMemo(() => {
    const c = { operational: 0, degraded: 0, down: 0, unknown: 0 };
    bars.forEach((b) => (c[b.status] += 1));
    return c;
  }, [bars]);

  return (
    <div className="relative">
      <div
        role="img"
        tabIndex={0}
        aria-label={`${label}: ${bars.length} periods. ${counts.operational} operational, ${counts.degraded} degraded, ${counts.down} down, ${counts.unknown} without data. Use the arrow keys to read each period.`}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight") setActive((i) => Math.min(bars.length - 1, (i ?? bars.length - 1) + 1));
          else if (e.key === "ArrowLeft") setActive((i) => Math.max(0, (i ?? bars.length) - 1));
          else if (e.key === "Escape") setActive(null);
        }}
        onBlur={() => setActive(null)}
        onMouseLeave={() => setActive(null)}
        className="flex items-stretch gap-px rounded-md sm:gap-[2px]"
        style={{ height }}
      >
        {bars.map((b, i) => (
          <div
            key={b.start}
            onMouseEnter={() => setActive(i)}
            className="group relative flex-1 cursor-default"
            style={{ minWidth: 1 }}
          >
            <div
              className={`absolute inset-0 rounded-[2px] transition-[filter,transform] duration-150 ${active === i ? "brightness-125" : ""}`}
              style={{
                background: barColor(b.status),
                opacity: b.status === "unknown" ? 1 : 0.35 + 0.65 * Math.min(1, b.coverage_pct / 100),
              }}
            />
          </div>
        ))}
      </div>

      <span className="sr-only" aria-live="polite">{hovered ? describe(hovered, window) : ""}</span>
      {hovered && (
        <div
          className="pointer-events-none absolute bottom-full z-20 mb-2.5 w-60 -translate-x-1/2 rounded-lg border border-line-strong bg-raised p-3 text-xs shadow-lg"
          style={{ left: `clamp(7.5rem, ${leftPct}%, calc(100% - 7.5rem))` }}
        >
          <p className="tabular text-muted">
            {clock(hovered.start, true)} – {clock(hovered.end, window !== "24h")}
          </p>
          <p className="mt-1.5 flex items-center gap-1.5 font-semibold text-ink">
            <StatusIcon status={hovered.status} className="h-3.5 w-3.5" />
            {hovered.status === "unknown" ? "No data recorded" : STATUS_META[hovered.status].label}
          </p>
          {hovered.status === "unknown" ? (
            <p className="mt-1 text-muted">No checks cover this period, so it is not counted as uptime.</p>
          ) : (
            <dl className="tabular mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-muted">
              <dt>Uptime</dt>
              <dd className="text-right text-ink">{hovered.uptime_pct?.toFixed(2)}%</dd>
              <dt>Coverage</dt>
              <dd className="text-right text-ink">{hovered.coverage_pct.toFixed(0)}%</dd>
              <dt>Checks</dt>
              <dd className="text-right text-ink">
                {hovered.checks}
                {hovered.failed_checks > 0 && <span className="text-muted"> · {hovered.failed_checks} failed</span>}
              </dd>
              {hovered.degraded_seconds > 0 && (
                <>
                  <dt>Degraded</dt>
                  <dd className="text-right text-ink">{duration(hovered.degraded_seconds)}</dd>
                </>
              )}
              {hovered.down_seconds > 0 && (
                <>
                  <dt>Down</dt>
                  <dd className="text-right text-ink">{duration(hovered.down_seconds)}</dd>
                </>
              )}
            </dl>
          )}
        </div>
      )}

      <div className={`flex justify-between text-[11px] text-faint ${compact ? "mt-1" : "mt-2"}`}>
        <span>{WINDOW_START_LABEL[window]}</span>
        <span>Now</span>
      </div>
    </div>
  );
}

export function StatusLegend() {
  const items: [Status, string][] = [["operational", "Operational"], ["degraded", "Degraded"], ["down", "Down"], ["unknown", "No data"]];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted">
      {items.map(([s, label]) => (
        <span key={s} className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-[3px]" style={{ background: barColor(s) }} />
          <StatusIcon status={s} className="h-3 w-3" />
          {label}
        </span>
      ))}
    </div>
  );
}

/* --------------------------------------------------------- latency chart */
type Point = { t: string; avg: number | null; max: number | null; samples: number };

function niceMax(v: number) {
  if (v <= 0) return 10;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const f = v / exp;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * exp;
}

export function LatencyChart({ title, points, height = 150 }: { title: string; points: Point[]; height?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const values = points.map((p) => p.avg).filter((v): v is number => v !== null);
  const top = niceMax(Math.max(...values, 1) * 1.15);
  const n = points.length;
  const x = (i: number) => (n <= 1 ? 0 : (i / (n - 1)) * 1000);
  const y = (v: number) => height - (v / top) * (height - 6) - 1;

  const { line, area, singles } = useMemo(() => {
    let line = "";
    let area = "";
    const singles: [number, number][] = []; // isolated points (neighbours missing) would draw nothing as a path
    let run: [number, number][] = [];
    const flush = () => {
      if (!run.length) return;
      if (run.length === 1) singles.push(run[0]);
      line += run.map(([px, py], j) => `${j ? "L" : "M"}${px.toFixed(1)},${py.toFixed(1)}`).join("");
      area += `M${run[0][0].toFixed(1)},${height}` + run.map(([px, py]) => `L${px.toFixed(1)},${py.toFixed(1)}`).join("") + `L${run[run.length - 1][0].toFixed(1)},${height}Z`;
      run = [];
    };
    points.forEach((p, i) => (p.avg === null ? flush() : run.push([x(i), y(p.avg)])));
    flush();
    return { line, area, singles };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [points, top, height]);

  const hp = hover !== null ? points[hover] : null;
  const avg = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;

  return (
    <figure className="min-w-0">
      <figcaption className="mb-3 flex items-baseline justify-between gap-3">
        <span className="text-sm font-medium text-ink">{title}</span>
        <span className="tabular text-xs text-muted">avg {ms(avg)}</span>
      </figcaption>
      <div className="flex gap-2">
        <div className="tabular flex flex-col justify-between pb-0.5 text-right text-[10.5px] text-faint" style={{ height }} aria-hidden="true">
          <span>{ms(top)}</span>
          <span>{ms(top / 2)}</span>
          <span>0</span>
        </div>
        <div
          className="relative flex-1"
          style={{ height }}
          onMouseLeave={() => setHover(null)}
          onMouseMove={(e) => {
            const r = e.currentTarget.getBoundingClientRect();
            const i = Math.round(((e.clientX - r.left) / r.width) * (n - 1));
            setHover(Math.max(0, Math.min(n - 1, i)));
          }}
        >
          <svg viewBox={`0 0 1000 ${height}`} preserveAspectRatio="none" className="absolute inset-0 h-full w-full overflow-visible" role="img" aria-label={`${title} line chart`}>
            <defs>
              <linearGradient id={`fill-${title.replace(/\W/g, "")}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0" stopColor="#225b46" stopOpacity="0.22" />
                <stop offset="1" stopColor="#225b46" stopOpacity="0" />
              </linearGradient>
            </defs>
            {[0, 0.5, 1].map((f) => (
              <line key={f} x1="0" x2="1000" y1={y(top * f)} y2={y(top * f)} stroke={f === 0 ? "#bcc3b5" : "#e1e5da"} strokeWidth="1" vectorEffect="non-scaling-stroke" />
            ))}
            <path d={area} fill={`url(#fill-${title.replace(/\W/g, "")})`} />
            <path d={line} fill="none" stroke="#225b46" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
            {hover !== null && <line x1={x(hover)} x2={x(hover)} y1="0" y2={height} stroke="#646a76" strokeWidth="1" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />}
          </svg>
          {singles.map(([px, py]) => (
            <span key={px} className="pointer-events-none absolute h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent" style={{ left: `${(px / 1000) * 100}%`, top: py }} />
          ))}
          {hp && hp.avg !== null && (
            <span
              className="pointer-events-none absolute h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent ring-2 ring-surface"
              style={{ left: `${(x(hover!) / 1000) * 100}%`, top: y(hp.avg) }}
            />
          )}
          {hp && (
            <div
              className="pointer-events-none absolute -top-2 z-10 w-44 -translate-x-1/2 -translate-y-full rounded-lg border border-line-strong bg-raised p-2.5 text-xs shadow-lg"
              style={{ left: `clamp(5.5rem, ${(x(hover!) / 1000) * 100}%, calc(100% - 5.5rem))` }}
            >
              <p className="text-muted">{clock(hp.t, true)}</p>
              {hp.avg === null ? (
                <p className="mt-1 text-muted">No successful samples</p>
              ) : (
                <p className="tabular mt-1 text-ink">
                  <span className="font-semibold">{ms(hp.avg)}</span> avg · {ms(hp.max)} max
                  <span className="block text-muted">{hp.samples} sample{hp.samples === 1 ? "" : "s"}</span>
                </p>
              )}
            </div>
          )}
          {!values.length && <div className="absolute inset-0 flex items-center justify-center text-xs text-faint">No latency samples in this window</div>}
        </div>
      </div>
      {n > 0 && (
        <div className="ml-12 mt-2 flex justify-between text-[10.5px] text-faint">
          <span>{clock(points[0].t, true)}</span>
          <span>Now</span>
        </div>
      )}
    </figure>
  );
}
