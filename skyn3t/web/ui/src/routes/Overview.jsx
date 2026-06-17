import React from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";

function Stat({ label, value, hint }) {
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wide text-slate-500">
        {label}
      </div>
      <div className="mt-1 text-2xl font-semibold text-slate-100">
        {value ?? "—"}
      </div>
      {hint ? <div className="mt-1 text-xs text-slate-500">{hint}</div> : null}
    </div>
  );
}

export default function Overview({ stream }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["health"],
    queryFn: queryFn("/health"),
  });

  const recent = (stream?.events || []).slice(-6).reverse();

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Overview</h1>
        <p className="text-sm text-slate-500">
          Live system health and recent activity.
        </p>
      </header>

      {error ? (
        <div className="card border-rose-800 text-sm text-rose-300">
          API unreachable: {String(error.message)}
        </div>
      ) : null}

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat
          label="Agents"
          value={isLoading ? "…" : data?.agents ?? data?.agent_count}
        />
        <Stat
          label="Active builds"
          value={isLoading ? "…" : data?.active_builds ?? 0}
        />
        <Stat
          label="Events"
          value={stream?.events?.length ?? 0}
          hint="this session"
        />
        <Stat
          label="Backend"
          value={data?.llm_backend ?? data?.backend ?? "stub"}
        />
      </div>

      <section className="card">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">
          Live event tail
        </h2>
        {recent.length === 0 ? (
          <p className="text-sm text-slate-500">No events yet.</p>
        ) : (
          <ul className="space-y-1 font-mono text-xs">
            {recent.map((e) => (
              <li key={e.id || e.timestamp} className="flex gap-3">
                <span className="text-brand">{e.type}</span>
                <span className="text-slate-500">{e.source}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
