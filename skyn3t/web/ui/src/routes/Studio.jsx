import React, { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";

// Canonical pipeline stages (mirrors the stage vocabulary in the backend).
const STAGES = [
  "brainstorm",
  "research",
  "architecture",
  "design",
  "codegen",
  "review",
  "verify_build",
  "package",
];

function stageState(stage, events) {
  // Derive a stage's state from BUILD_STAGE_* events in the stream.
  let state = "pending";
  events.forEach((e) => {
    const s = (e.payload && (e.payload.stage || e.payload.capability)) || "";
    if (s !== stage) return;
    if (e.type === "BUILD_STAGE_STARTED") state = "running";
    if (e.type === "BUILD_STAGE_COMPLETED") state = "done";
    if (e.type === "TASK_FAILED" || e.type === "BUILD_FAILED") state = "failed";
  });
  return state;
}

const DOT = {
  pending: "bg-slate-700",
  running: "bg-amber-400 animate-pulseglow",
  done: "bg-emerald-500",
  failed: "bg-rose-500",
};

export default function Studio({ stream }) {
  const qc = useQueryClient();
  const [brief, setBrief] = useState("");

  const { data: builds } = useQuery({
    queryKey: ["builds"],
    queryFn: queryFn("/builds"),
  });

  const submit = useMutation({
    mutationFn: (payload) => apiPost("/builds", payload),
    onSuccess: () => {
      setBrief("");
      qc.invalidateQueries({ queryKey: ["builds"] });
    },
  });

  const events = stream?.events || [];
  const pipeline = useMemo(
    () => STAGES.map((s) => ({ stage: s, state: stageState(s, events) })),
    [events]
  );

  const recentBuilds = Array.isArray(builds) ? builds : builds?.builds || [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Studio</h1>
        <p className="text-sm text-slate-500">
          Submit a build brief and watch the pipeline.
        </p>
      </header>

      <form
        className="card flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (brief.trim()) submit.mutate({ brief: brief.trim() });
        }}
      >
        <input
          className="flex-1 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm outline-none focus:border-brand"
          placeholder="Describe the app to build…"
          value={brief}
          onChange={(e) => setBrief(e.target.value)}
        />
        <button
          type="submit"
          disabled={submit.isPending || !brief.trim()}
          className="rounded-lg bg-brand px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-50"
        >
          {submit.isPending ? "Submitting…" : "Build"}
        </button>
      </form>

      {submit.isError ? (
        <div className="card border-rose-800 text-sm text-rose-300">
          {String(submit.error.message)}
        </div>
      ) : null}

      <section className="card">
        <h2 className="mb-4 text-sm font-semibold text-slate-300">
          Pipeline (live)
        </h2>
        <ol className="flex flex-wrap items-center gap-x-2 gap-y-3">
          {pipeline.map((p, i) => (
            <li key={p.stage} className="flex items-center gap-2">
              <span className={`h-3 w-3 rounded-full ${DOT[p.state]}`} />
              <span className="text-xs text-slate-300">{p.stage}</span>
              {i < pipeline.length - 1 ? (
                <span className="text-slate-700">→</span>
              ) : null}
            </li>
          ))}
        </ol>
      </section>

      <section className="card">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">
          Recent builds
        </h2>
        {recentBuilds.length === 0 ? (
          <p className="text-sm text-slate-500">No builds yet.</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="text-xs uppercase text-slate-500">
              <tr>
                <th className="py-1">Slug</th>
                <th className="py-1">Status</th>
                <th className="py-1">Score</th>
                <th className="py-1">Cost</th>
              </tr>
            </thead>
            <tbody>
              {recentBuilds.map((b) => (
                <tr key={b.build_id || b.slug} className="border-t border-slate-800">
                  <td className="py-1.5 font-mono text-xs">{b.slug}</td>
                  <td className="py-1.5">{b.status}</td>
                  <td className="py-1.5">{b.score ?? "—"}</td>
                  <td className="py-1.5">
                    {b.cost_usd != null ? `$${Number(b.cost_usd).toFixed(4)}` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}
