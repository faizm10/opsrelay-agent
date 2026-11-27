"use client";

import { ArrowUpRight, Brain, GitPullRequest, Search } from "lucide-react";
import { type Learning, type LearningStrategy, usePoll } from "@/lib/api";
import { ago, useNow } from "@/lib/format";

/** Fix playbooks from agents/rl_policy.py, in plain words. */
const PLAYBOOKS: Record<string, string> = {
  fix_quantity_multiplier: "Fix line-total quantity math",
  fix_discount_divisor: "Fix percentage discount (÷100)",
  fix_stock_comparison: "Block orders above stock",
  fix_idempotency_guard: "Fix idempotency-key guard",
  fix_recipient_delivery: "Delivery needs recipient online",
  fix_full_timestamp_sort: "Sort messages by full timestamp",
  revert_last_commit: "Revert the last commit",
  add_defensive_guard: "Add a defensive guard",
  retry_flaky_tests: "Retry as a flaky test",
  increase_timeout: "Raise the timeout",
};

const BUGS: Record<string, string> = {
  quantity: "Quantity",
  discount: "Discount",
  inventory: "Inventory",
  flaky_checkout: "Flaky checkout test",
  duplicate: "Duplicate messages",
  status: "Delivery status",
  ordering: "Message ordering",
  ws_timeout: "WebSocket timeout",
};

const APPS: Record<string, string> = { shopping: "NovaMart (shopping)", messaging: "NovaConnect (messaging)" };

const pct0 = (v: number | null | undefined) => (v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`);
const label = (action: string | null) => (action ? PLAYBOOKS[action] ?? action : "—");

function rateColor(rate: number) {
  return rate >= 0.6 ? "bg-ok" : rate >= 0.3 ? "bg-warn" : "bg-crit/70";
}

function StrategyList({ app, rows }: { app: string; rows: LearningStrategy[] }) {
  return (
    <article className="card p-4">
      <h3 className="text-sm font-semibold text-ink">{APPS[app] ?? app}</h3>
      <p className="mt-0.5 text-xs text-muted">Success rate of each fix strategy the agent has tried</p>
      <ul className="mt-4 space-y-2.5">
        {rows.map((r) => (
          <li key={r.action} className="text-sm">
            <div className="flex items-baseline gap-2">
              <span className="min-w-0 flex-1 truncate text-ink" title={r.action}>{label(r.action)}</span>
              <span className="tabular font-medium text-ink">{pct0(r.success_rate)}</span>
              <span className="tabular w-24 text-right text-[11px] text-faint">
                {r.tries} tries{r.real_tries ? ` · ${r.real_tries} real` : ""}
              </span>
            </div>
            <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-line" role="presentation">
              <div className={`h-full rounded-full ${rateColor(r.success_rate)}`} style={{ width: `${Math.max(2, r.success_rate * 100)}%` }} />
            </div>
          </li>
        ))}
      </ul>
    </article>
  );
}

export function LearningPanel() {
  const { data } = usePoll<Learning>("/api/v1/learning", 30_000);
  const now = useNow();
  if (!data) return null;

  if (!data.available) {
    return (
      <section className="panel p-5 sm:p-6" aria-labelledby="learning-title">
        <div className="flex items-center gap-2">
          <Brain className="h-4 w-4 text-accent" />
          <h2 id="learning-title" className="text-sm font-semibold">Fix history</h2>
        </div>
        <p className="mt-2 text-sm text-muted">
          The repair agent has no learning history yet. Train it with{" "}
          <code className="rounded-sm bg-raised px-1.5 py-0.5 font-mono text-xs text-ink">python -m agents.rl_policy seed</code> then{" "}
          <code className="rounded-sm bg-raised px-1.5 py-0.5 font-mono text-xs text-ink">train</code> (requires MongoDB).
        </p>
      </section>
    );
  }

  const t = data.training;
  const byApp = data.strategies.reduce<Record<string, LearningStrategy[]>>((acc, s) => {
    (acc[s.app] ??= []).push(s);
    return acc;
  }, {});
  const ready = data.vector_indexes.filter((v) => v.status === "READY");

  return (
    <section className="panel p-5 sm:p-6" aria-labelledby="learning-title">
      <div className="flex flex-wrap items-center gap-2">
        <Brain className="h-4 w-4 text-accent" />
        <h2 id="learning-title" className="text-sm font-semibold">Fix history</h2>
        <span className="ml-auto text-xs text-faint">{t ? `Last trained ${ago(t.at, now)}` : "Not trained yet"}</span>
      </div>
      <p className="mt-1 text-xs text-muted">
        The agent learns which fix works for which failure from rewards (reinforcement learning), stored in MongoDB Atlas.
      </p>

      <dl className="tabular mt-5 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <div className="card p-4">
          <dt className="label">First-pick accuracy</dt>
          <dd className="mt-1 text-lg font-semibold text-ink tabular">
            {t ? (
              <>
                <span className="text-base text-faint line-through decoration-faint/60">{pct0(t.accuracy_before)}</span>{" "}
                <span className="text-ok">{pct0(t.accuracy_after)}</span>
              </>
            ) : "—"}
          </dd>
          <dd className="mt-0.5 text-[11px] text-faint">
            {t ? `before → after training, on ${t.eval_incidents ?? "unseen"} unseen incidents` : "run training to measure"}
          </dd>
        </div>
        <div className="card p-4">
          <dt className="label">Fix attempts learned from</dt>
          <dd className="mt-1 text-lg font-semibold text-ink tabular">{(data.totals.experiences ?? 0).toLocaleString()}</dd>
          <dd className="mt-0.5 text-[11px] text-faint">{data.totals.real_experiences ?? 0} from real incidents · rl_experiences</dd>
        </div>
        <div className="card p-4">
          <dt className="label">Training incidents</dt>
          <dd className="mt-1 text-lg font-semibold text-ink tabular">{data.totals.mock_incidents ?? 0}</dd>
          <dd className="mt-0.5 text-[11px] text-faint">mock_incidents · {data.totals.memories ?? 0} fixes in memory</dd>
        </div>
        <div className="card p-4">
          <dt className="label">Atlas Vector Search</dt>
          <dd className="mt-1 flex items-center gap-2 text-lg font-semibold text-ink tabular">
            <Search className="h-5 w-5 text-muted" />
            {data.vector_indexes.length ? `${ready.length}/${data.vector_indexes.length} ready` : "—"}
          </dd>
          <dd className="mt-0.5 truncate text-[11px] text-faint" title={data.vector_indexes.map((v) => `${v.name}: ${v.status}`).join(", ")}>
            {data.vector_indexes.length ? data.vector_indexes.map((v) => v.name).join(" · ") : "indexes visible on Atlas only"}
          </dd>
        </div>
      </dl>

      {t && t.curve.length > 0 && (
        <div className="mt-5 card p-4">
          <div className="flex flex-wrap items-baseline gap-2">
            <h3 className="text-sm font-semibold text-ink">Learning curve</h3>
            <span className="text-xs text-muted">incidents fixed on the first try, per 25 training incidents</span>
          </div>
          <div className="mt-4 flex h-28 items-end gap-2" role="img"
            aria-label={`First-try fix rate rose from ${pct0(t.curve[0].first_try_rate)} to ${pct0(t.curve[t.curve.length - 1].first_try_rate)}`}>
            {t.curve.map((p, i) => (
              <div key={i} className="flex h-full flex-1 flex-col items-center justify-end gap-1" title={`Pass ${p.epoch}, incidents ${p.incidents}: ${pct0(p.first_try_rate)} first try, ${p.avg_attempts} attempts on average`}>
                <span className="tabular text-[11px] text-muted">{pct0(p.first_try_rate)}</span>
                <div className="w-full rounded-t-md bg-accent/80" style={{ height: `${Math.max(4, p.first_try_rate * 100)}%` }} />
              </div>
            ))}
          </div>
          <div className="mt-1 flex gap-2 border-t border-line pt-1.5 text-[10px] text-faint">
            {t.curve.map((p, i) => (
              <span key={i} className="flex-1 text-center">{i === 0 || t.curve[i - 1].epoch !== p.epoch ? `pass ${p.epoch}` : ""}</span>
            ))}
          </div>
          {Object.keys(t.per_bug).length > 0 && (
            <p className="mt-3 text-xs leading-relaxed text-muted">
              After training, by bug:{" "}
              {Object.entries(t.per_bug).map(([bug, acc], i) => (
                <span key={bug}>
                  {i > 0 && " · "}
                  {BUGS[bug] ?? bug} <span className={acc >= 0.6 ? "text-ok" : "text-warn"}>{pct0(acc)}</span>
                </span>
              ))}
            </p>
          )}
        </div>
      )}

      <div className="mt-5 grid gap-4 lg:grid-cols-2">
        {Object.entries(byApp).map(([app, rows]) => (
          <StrategyList key={app} app={app} rows={rows} />
        ))}
      </div>

      {data.decisions.length > 0 && (
        <div className="mt-5 card p-4">
          <h3 className="text-sm font-semibold text-ink">Recent decisions on real incidents</h3>
          <ul className="mt-3 divide-y divide-line text-sm">
            {data.decisions.map((d) => (
              <li key={d.incident_id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
                <span className="text-muted">{d.app ?? "—"} · {d.source}</span>
                <span className="min-w-0 flex-1 text-ink">{label(d.action)}</span>
                <span className="tabular text-xs text-muted">expected {pct0(d.expected_success)}</span>
                <span className="text-xs text-faint">{d.hinted ? "shown to agent" : "too little evidence"}</span>
                {d.pr_url && (
                  <a href={d.pr_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
                    <GitPullRequest className="h-3.5 w-3.5" /> PR <ArrowUpRight className="h-3 w-3" />
                  </a>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="mt-4 text-[11px] leading-relaxed text-faint">
        Training rewards come from a simulator over mock incidents. Real rewards come from tested patches and from humans merging or closing the agent&apos;s pull requests.
      </p>
    </section>
  );
}
