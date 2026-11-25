"use client";

import { isPublicPage } from "@/lib/routes";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { MessageCircle, RotateCcw, Send, X } from "lucide-react";
import { networkError, responseError } from "@/lib/api";

type Message = { role: "user" | "assistant"; content: string };

const SUGGESTIONS = ["What's broken right now?", "What is the investigation doing?", "Is it fixed yet?", "What should I do next?"];

/** Floating chat about current incidents. On an incident page it focuses on that incident. */
export function AssistantChat() {
  const pathname = usePathname();
  const match = /^\/incidents\/([^/]+)/.exec(pathname ?? "");
  const incidentId = match ? decodeURIComponent(match[1]) : null;
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages, pending]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  async function send(text: string) {
    const content = text.trim();
    if (!content || pending) return;
    const next: Message[] = [...messages, { role: "user", content }];
    setMessages(next);
    setDraft("");
    setError("");
    setPending(true);
    try {
      const response = await fetch("/api/v1/assistant/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: next.slice(-20), incident_id: incidentId }),
      });
      if (!response.ok) {
        setError((await responseError(response, "POST /api/v1/assistant/chat")).message);
        return;
      }
      const body = await response.json();
      setMessages([...next, { role: "assistant", content: body.data.reply }]);
    } catch {
      setError(networkError("POST /api/v1/assistant/chat").message);
    } finally {
      setPending(false);
    }
  }

  if (isPublicPage(pathname)) return null;
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="fixed bottom-5 right-5 z-40 inline-flex items-center gap-2 rounded-full bg-ink px-4 py-2.5 text-sm font-medium text-white shadow-md transition hover:bg-ink/85"
      >
        <MessageCircle className="h-4 w-4" aria-hidden="true" /> Ask the assistant
      </button>
    );
  }

  return (
    <section
      className="fixed inset-x-4 bottom-4 z-40 flex max-h-[min(640px,calc(100vh-2rem))] flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl sm:inset-x-auto sm:right-5 sm:w-[400px]"
      aria-label="Assistant chat"
    >
      <header className="flex items-center gap-3 border-b border-line bg-raised px-4 py-4">
        <MessageCircle className="h-4 w-4 text-accent" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold">Ask the assistant</h2>
          <p className="truncate text-xs text-faint">{incidentId ? "About this incident" : "About what's happening now"}</p>
        </div>
        {messages.length > 0 && (
          <button type="button" onClick={() => { setMessages([]); setError(""); }} className="rounded-md p-1.5 text-muted hover:text-ink" aria-label="Start a new chat">
            <RotateCcw className="h-4 w-4" />
          </button>
        )}
        <button type="button" onClick={() => setOpen(false)} className="rounded-md p-1.5 text-muted hover:text-ink" aria-label="Close chat">
          <X className="h-4 w-4" />
        </button>
      </header>

      <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4 text-sm" aria-live="polite">
        {messages.length === 0 && (
          <div>
            <p className="text-muted">Ask anything about the outage, the investigation's progress, or what to do next.</p>
            <div className="mt-3 flex flex-wrap gap-2">
              {SUGGESTIONS.map((s) => (
                <button key={s} type="button" onClick={() => send(s)} className="rounded-full border border-line px-3 py-1.5 text-xs text-muted transition hover:border-accent/60 hover:text-ink">
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, n) => (
          <div key={n} className={m.role === "user" ? "flex justify-end" : "flex"}>
            <p className={`max-w-[85%] whitespace-pre-wrap break-words rounded-lg px-3 py-2 leading-relaxed ${m.role === "user" ? "bg-accent/15 text-ink" : "bg-raised text-ink"}`}>
              {m.content}
            </p>
          </div>
        ))}
        {pending && <p className="text-xs text-faint">Thinking…</p>}
        {error && <p className="text-xs text-crit" role="alert">{error}</p>}
        <div ref={endRef} />
      </div>

      <form
        className="flex items-end gap-2 border-t border-line p-3"
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
      >
        <textarea
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(draft);
            }
          }}
          rows={1}
          maxLength={4000}
          placeholder="Ask a question…"
          className="max-h-32 min-h-10 flex-1 resize-none rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink placeholder:text-faint focus:border-accent focus:outline-none"
          aria-label="Your question"
        />
        <button type="submit" disabled={pending || !draft.trim()} className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-accent text-page transition disabled:opacity-40" aria-label="Send">
          <Send className="h-4 w-4" />
        </button>
      </form>
    </section>
  );
}
