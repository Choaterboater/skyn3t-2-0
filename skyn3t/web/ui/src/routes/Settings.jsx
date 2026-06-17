import React, { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between border-b border-slate-800 py-2 text-sm">
      <span className="text-slate-400">{label}</span>
      <span className="font-mono text-slate-200">{String(value)}</span>
    </div>
  );
}

export default function Settings() {
  const { data, error } = useQuery({
    queryKey: ["settings"],
    queryFn: queryFn("/settings"),
    retry: 0,
  });

  const [token, setToken] = useState(
    typeof localStorage !== "undefined"
      ? localStorage.getItem("skyn3t_token") || ""
      : ""
  );
  const [saved, setSaved] = useState(false);

  function save() {
    if (typeof localStorage !== "undefined") {
      if (token) localStorage.setItem("skyn3t_token", token);
      else localStorage.removeItem("skyn3t_token");
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  // Only show a curated, non-secret subset of the runtime settings.
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

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-slate-500">
          Runtime configuration (read-only) and dashboard auth token.
        </p>
      </header>

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
            onClick={save}
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
