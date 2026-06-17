import React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";

export default function Cortex() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["proposals"],
    queryFn: queryFn("/cortex/proposals"),
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }) =>
      apiPost(`/cortex/proposals/${id}/decide`, { decision }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }),
  });

  const proposals = Array.isArray(data) ? data : data?.proposals || [];

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold">Cortex</h1>
        <p className="text-sm text-slate-500">
          Self-evolution proposals awaiting decision.
        </p>
      </header>

      {error ? (
        <div className="card border-rose-800 text-sm text-rose-300">
          {String(error.message)}
        </div>
      ) : null}

      {isLoading ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : proposals.length === 0 ? (
        <p className="text-sm text-slate-500">No open proposals.</p>
      ) : (
        <div className="space-y-3">
          {proposals.map((p) => (
            <div key={p.id} className="card">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="font-medium text-slate-100">
                    {p.title || p.kind || `Proposal ${p.id}`}
                  </div>
                  <p className="mt-1 text-sm text-slate-400">
                    {p.summary || p.description}
                  </p>
                  {p.risk ? (
                    <span className="badge mt-2 bg-slate-800 text-slate-400">
                      risk: {p.risk}
                    </span>
                  ) : null}
                </div>
                <div className="flex shrink-0 gap-2">
                  <button
                    onClick={() => decide.mutate({ id: p.id, decision: "approve" })}
                    disabled={decide.isPending}
                    className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
                  >
                    Approve
                  </button>
                  <button
                    onClick={() => decide.mutate({ id: p.id, decision: "reject" })}
                    disabled={decide.isPending}
                    className="rounded-lg bg-rose-600 px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
                  >
                    Reject
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
