"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useId, useRef, useState } from "react";
import { X } from "lucide-react";
import { STATUS_POLL_MS, type Summary, usePoll } from "@/lib/api";
import { type MascotTone, mascotMessage } from "@/lib/mascot";

const TONE: Record<MascotTone, { light: string; ring: string }> = {
  ok: { light: "#91cf95", ring: "border-ok/40" },
  warn: { light: "#e5a000", ring: "border-warn/50" },
  crit: { light: "#e5484d", ring: "border-crit/50" },
  accent: { light: "#83b796", ring: "border-accent/50" },
};
const CLOSED_KEY = "opsrelay-mascot-closed";

function readClosed(): string | null {
  try {
    return window.sessionStorage.getItem(CLOSED_KEY);
  } catch {
    return null;
  }
}

/** Relay, the OpsRelay companion: one short, state-derived hint at a time, bottom-right of the signed-in dashboard. */
export function Mascot() {
  const pathname = usePathname();
  const signedIn = pathname !== "/sign-in";
  const { data } = usePoll<Summary>(signedIn ? "/api/v1/summary?window=24h" : null, STATUS_POLL_MS);
  const message = data ? mascotMessage(data) : null;
  const [open, setOpen] = useState(false);
  const shownKey = useRef<string | null>(null);
  const bubbleId = useId();

  // Open only when the situation changes, never on a poll that finds the same one, and not for
  // a situation the viewer already closed this session. Keys include the workspace id.
  useEffect(() => {
    if (!message || message.key === shownKey.current) return;
    shownKey.current = message.key;
    setOpen(message.tone !== "ok" && readClosed() !== message.key);
  }, [message?.key]); // the key, not the object: every poll builds a new message object

  const close = () => {
    setOpen(false);
    try {
      if (message) window.sessionStorage.setItem(CLOSED_KEY, message.key);
    } catch {
      // private mode: the bubble just reopens after a reload
    }
  };

  if (!signedIn || !message) return null;
  const tone = TONE[message.tone];
  const attention = message.tone !== "ok";
  return (
    <div
      className="fixed bottom-20 right-4 z-30 flex flex-col items-end gap-2 sm:right-5"
      onKeyDown={(e) => e.key === "Escape" && open && close()}
    >
      {open && (
        <div
          id={bubbleId} role="status" aria-live="polite"
          className={`relative w-[min(18rem,calc(100vw-2rem))] rounded-lg border ${tone.ring} bg-raised p-3 pr-8 text-xs shadow-lg motion-safe:animate-rise sm:text-sm`}
        >
          <p className="text-ink">{message.text}</p>
          {message.links.length > 0 && (
            <p className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
              {message.links.map((l) => l.external ? (
                <a key={l.href} href={l.href} target="_blank" rel="noreferrer" className="font-medium text-accent hover:underline">{l.label}</a>
              ) : (
                <Link key={l.href} href={l.href} className="font-medium text-accent hover:underline">{l.label}</Link>
              ))}
            </p>
          )}
          <button type="button" onClick={close} aria-label="Hide Relay's message" className="absolute right-1.5 top-1.5 rounded p-1 text-faint hover:text-ink">
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </button>
        </div>
      )}
      <button
        type="button" onClick={() => (open ? close() : setOpen(true))}
        aria-expanded={open} aria-controls={open ? bubbleId : undefined}
        aria-label={open ? "Hide Relay's message" : `Show Relay's message${attention ? " (needs attention)" : ""}`}
        className="rounded-xl focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        <svg viewBox="0 0 44 48" className="h-10 w-9 sm:h-12 sm:w-11" aria-hidden="true">
          <line x1="22" y1="4" x2="22" y2="11" stroke="#42644e" strokeWidth="2" strokeLinecap="round" />
          <circle cx="22" cy="4" r="3" fill={tone.light} />
          {attention && (
            <circle cx="22" cy="4" r="3" fill={tone.light} className="origin-[22px_4px] motion-safe:animate-ping2 motion-reduce:hidden" />
          )}
          <rect x="3" y="11" width="38" height="34" rx="10" fill="#253c31" stroke="#42644e" strokeWidth="1.5" />
          <circle cx="15" cy="23" r="2.4" fill="#f2f3d6" />
          <circle cx="29" cy="23" r="2.4" fill="#f2f3d6" />
          <path d="M10 34h6l2.5-4 3.5 7 2.5-5h9.5" fill="none" stroke={tone.light} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
    </div>
  );
}
