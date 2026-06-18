import React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill, Empty } from "../components/ui.jsx";

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
    <div>
      <PageHeader
        eyebrow="Foundry · Autonomy Inbox"
        title="Cortex"
        sub="Self-evolution proposals awaiting your decision. The swarm wants to reshape itself."
        actions={
          <span className="badge border-hairline text-ash">
            open · <span className="ml-1 text-ember">{proposals.length}</span>
          </span>
        }
      />

      {error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Cortex unreachable: {String(error.message)}
        </Panel>
      ) : null}

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-3">
        <Stat
          label="Pending"
          value={proposals.length}
          tone={proposals.length ? "ember" : "plasma"}
          hint={proposals.length ? "awaiting review" : "inbox clear"}
        />
        <Stat label="Source" value="Cortex" hint="autonomy engine" />
        <Stat label="Channel" value="/cortex" hint="proposals" />
      </div>

      <Panel>
        <PanelHead
          label="Proposal inbox"
          right={
            <span className="font-mono text-[11px] text-ash">
              {decide.isPending ? "deciding…" : `${proposals.length} open`}
            </span>
          }
        />
        {isLoading ? (
          <Empty icon="≋">Listening to Cortex…</Empty>
        ) : proposals.length === 0 ? (
          <Empty icon="◇">No open proposals. The swarm is content for now.</Empty>
        ) : (
          <div className="divide-y divide-hairline/60">
            {proposals.map((p) => {
              const id = p.id ?? p.proposal_id;
              return (
                <div key={id} className="px-4 py-4">
                  <div className="flex items-start justify-between gap-4">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        {p.kind ? <Pill tone="ember">{p.kind}</Pill> : null}
                        {p.risk ? (
                          <Pill tone="ash">risk · {p.risk}</Pill>
                        ) : null}
                      </div>
                      <div className="mt-2 font-display text-base font-semibold text-bone">
                        {p.title || p.kind || `Proposal ${id}`}
                      </div>
                      <p className="mt-1 max-w-2xl font-sans text-sm text-ash">
                        {p.summary || p.description}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      <button
                        onClick={() => decide.mutate({ id, decision: "approve" })}
                        disabled={decide.isPending}
                        className="btn-ember disabled:opacity-50"
                      >
                        Approve
                      </button>
                      <button
                        onClick={() => decide.mutate({ id, decision: "reject" })}
                        disabled={decide.isPending}
                        className="btn-ghost disabled:opacity-50"
                      >
                        Reject
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
