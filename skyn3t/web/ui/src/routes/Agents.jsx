import React from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn } from "../api.js";

const STATUS_BADGE = {
  idle: "bg-slate-700 text-slate-300",
  running: "bg-amber-500/20 text-amber-300",
  busy: "bg-amber-500/20 text-amber-300",
  healthy: "bg-emerald-500/20 text-emerald-300",
  failed: "bg-rose-500/20 text-rose-300",
  unregistered: "bg-slate-800 text-slate-500",
};

export default function Agents({ stream }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["agents"],
    queryFn: queryFn("/agents"),
  });

  // Track recent per-agent activity from the live stream.
  const lastBySource = {};
  (stream?.events || []).forEach((e) => {
    if (e.source) lastBySource[e.source] = e.type;
  });

  const agents = Array.isArray(data) ? data : data?.agents || [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Swarm</h1>
        <p className="text-sm text-slate-500">
          Registered agents and their live status.
        </p>
      </header>

      {error ? (
        <div className="card border-rose-800 text-sm text-rose-300">
          {String(error.message)}
        </div>
      ) : null}

      {isLoading ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : agents.length === 0 ? (
        <p className="text-sm text-slate-500">No agents registered.</p>
      ) : (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {agents.map((a) => {
            const status = a.status || "idle";
            return (
              <div key={a.name} className="card">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-100">{a.name}</span>
                  <span
                    className={`badge ${
                      STATUS_BADGE[status] || STATUS_BADGE.idle
                    }`}
                  >
                    {status}
                  </span>
                </div>
                <div className="mt-1 text-xs text-slate-500">
                  {a.agent_type || a.type} · {a.provider || "local"}
                </div>
                {lastBySource[a.name] ? (
                  <div className="mt-2 font-mono text-xs text-brand">
                    {lastBySource[a.name]}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
