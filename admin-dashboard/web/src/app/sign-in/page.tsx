"use client";

import { Button } from "@astryxdesign/core/Button";
import { useEffect, useState } from "react";
import { type Account, selectWorkspace, send } from "@/lib/api";
import { Logo } from "@/components/ui";

export default function SignInPage() {
  const [mode, setMode] = useState<"sign-in" | "sign-up" | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");

  const enter = async (account: Account) => {
    const id = account.workspaces[0]?.id ?? (await send<{ id: string }>("POST", "/api/v1/workspaces", { name: "My account" })).id;
    if (!account.workspace_id) selectWorkspace(id);
    window.location.assign("/");
  };

  useEffect(() => {
    fetch("/api/v1/auth/me", { cache: "no-store" })
      .then(async (res) => (res.ok ? await enter((await res.json()).data) : setMode("sign-in")))
      .catch((err) => { setMode("sign-in"); setError((err as Error).message); });
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setPending(true);
    setError("");
    try {
      await enter(await send<Account>("POST", `/api/v1/auth/${mode}`, { email, password }));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPending(false);
    }
  };

  if (!mode) return null;
  const title = { "sign-in": "Sign in to OpsRelay", "sign-up": "Create your OpsRelay account" }[mode];
  return (
    <main className="grid min-h-screen place-items-center bg-page px-4 py-10 text-ink">
      <form onSubmit={submit} className="panel w-full max-w-md space-y-5 p-6 sm:p-10">
        <Logo />
        <h1 className="page-title !text-5xl">{title}</h1>
        <label className="block space-y-1.5">
          <span className="label">Email</span>
          <input className="input" type="email" autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} required autoFocus />
        </label>
        <label className="block space-y-1.5">
          <span className="label">Password</span>
          <input
            className="input" type="password" minLength={8} required value={password} onChange={(e) => setPassword(e.target.value)}
            autoComplete={mode === "sign-up" ? "new-password" : "current-password"}
          />
        </label>
        {error && <p className="text-sm text-crit" role="alert">{error}</p>}
        <Button type="submit" variant="primary" size="lg" width="100%" isLoading={pending}
          label={mode === "sign-up" ? "Create account" : "Sign in"} />
        <button
          type="button" className="w-full text-center text-xs text-muted hover:text-ink"
          onClick={() => { setMode(mode === "sign-in" ? "sign-up" : "sign-in"); setError(""); }}
        >
          {mode === "sign-in" ? "No account yet? Create one" : "Already have an account? Sign in"}
        </button>
      </form>
    </main>
  );
}
