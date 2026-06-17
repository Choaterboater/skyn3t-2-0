import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-800 py-2 text-sm">
      <span className="text-slate-400">{label}</span>
      <span className="font-mono text-slate-200">{String(value)}</span>
    </div>
  );
}

const BACKENDS = ["auto", "stub", "claude_cli", "kimi_cli", "copilot_cli", "openrouter"];
const PROVIDERS = ["openrouter", "anthropic", "openai", "kimi"];

export default function Settings() {
  const { data, error } = useQuery({
    queryKey: ["settings"],
    queryFn: queryFn("/settings"),
    retry: 0,
  });

  const secrets = useQuery({
    queryKey: ["llm-secrets"],
    queryFn: queryFn("/llm/secrets"),
    retry: 0,
    refetchInterval: 4000,
  });

  const [token, setToken] = useState(
    typeof localStorage !== "undefined"
      ? localStorage.getItem("skyn3t_token") || ""
      : ""
  );
  const [saved, setSaved] = useState(false);

  const [provider, setProvider] = useState("openrouter");
  const [key, setKey] = useState("");
  const [msg, setMsg] = useState("");

  function saveToken() {
    if (typeof localStorage !== "undefined") {
      if (token) localStorage.setItem("skyn3t_token", token);
      else localStorage.removeItem("skyn3t_token");
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  async function saveKey() {
    try {
      const r = await apiPost("/llm/key", { provider, key });
      setKey("");
      setMsg(`${provider}: ${r.configured ? "saved" : "cleared"} → backend ${r.backend}`);
      secrets.refetch();
    } catch (e) {
      setMsg(String(e.message));
    }
  }

  async function pickBackend(b) {
    try {
      const r = await apiPost("/llm/backend", { backend: b });
      setMsg(`backend → ${r.active}`);
      secrets.refetch();
    } catch (e) {
      setMsg(String(e.message));
    }
  }

  const flags = data
    ? {
        free_only: data.free_only,
        no_claude: data.no_claude,
        execution_backend: data.execution_backend,
        autonomous_builds: data.autonomous_builds,
        approval_gates: data.approval_gates,
        per_build_usd_cap: data.per_build_usd_cap,
        daily_usd_cap: data.daily_usd_cap,
      }
    : null;

  const active = secrets.data?.backend;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-slate-500">
          LLM backend &amp; keys, runtime config, and dashboard auth token.
        </p>
      </header>

      <section className="card">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">LLM backend</h2>
          <span className="rounded-full bg-slate-800 px-3 py-1 font-mono text-xs text-emerald-300">
            active: {active || "…"}
          </span>
        </div>
        <p className="mb-3 text-xs text-slate-500">
          <b>auto</b> uses OpenRouter if a key is set, else a local CLI
          (claude/kimi/copilot), else the offline stub. Pick one to pin it.
        </p>
        <div className="flex flex-wrap gap-2">
          {BACKENDS.map((b) => (
            <button
              key={b}
              onClick={() => pickBackend(b)}
              className={`rounded-lg px-3 py-1.5 text-xs font-semibold ${
                secrets.data?.backend_pref === b
                  ? "bg-brand text-slate-950"
                  : "border border-slate-700 text-slate-300 hover:border-brand"
              }`}
            >
              {b}
            </button>
          ))}
        </div>
      </section>

      <section className="card">
        <h2 className="mb-2 text-sm font-semibold text-slate-300">API key</h2>
        <p className="mb-3 text-xs text-slate-500">
          Stored in the server&apos;s <code>.env</code>. Setting an OpenRouter
          key switches <b>auto</b> to real cloud generation immediately.
        </p>
        <div className="flex flex-wrap gap-2">
          <select
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm"
          >
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {p}
                {secrets.data?.providers?.[p] ? " ✓" : ""}
              </option>
            ))}
          </select>
          <input
            type="password"
            className="min-w-[12rem] flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-brand"
            placeholder={`${provider} API key`}
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
          <button
            onClick={saveKey}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-slate-950"
          >
            Save key
          </button>
        </div>
        {msg ? <p className="mt-2 text-xs text-emerald-300">{msg}</p> : null}
      </section>

      <section className="card">
        <h2 className="mb-2 text-sm font-semibold text-slate-300">
          API auth token
        </h2>
        <p className="mb-3 text-xs text-slate-500">
          Stored locally in your browser and sent as a Bearer token.
        </p>
        <div className="flex gap-2">
          <input
            type="password"
            className="flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-brand"
            placeholder="auth token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button
            onClick={saveToken}
            className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-slate-950"
          >
            {saved ? "Saved" : "Save"}
          </button>
        </div>
      </section>

      <section className="card">
        <h2 className="mb-2 text-sm font-semibold text-slate-300">Runtime</h2>
        {error ? (
          <p className="text-sm text-rose-300">{String(error.message)}</p>
        ) : flags ? (
          Object.entries(flags).map(([k, v]) => (
            <Row key={k} label={k} value={v} />
          ))
        ) : (
          <p className="text-sm text-slate-500">Loading…</p>
        )}
      </section>
    </div>
  );
}
