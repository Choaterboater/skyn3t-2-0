import React, { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill, Empty } from "../components/ui.jsx";

export default function Cortex() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [confirmClearAll, setConfirmClearAll] = useState(false);
  const { data, isLoading, error } = useQuery({
    queryKey: ["proposals"],
    // Inbox = only proposals genuinely awaiting a decision (not already
    // approved/rejected/deduped ones lingering in the cache).
    queryFn: queryFn("/cortex/proposals?status=pending"),
  });

  const decide = useMutation({
    mutationFn: ({ id, decision }) =>
      apiPost(`/cortex/proposals/${id}/decide`, { decision }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }),
  });

  const clear = useMutation({
    mutationFn: (scope) => apiPost("/cortex/proposals/clear", { scope }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }),
  });

  const scout = useMutation({
    mutationFn: () => apiPost("/cortex/scout", {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["proposals"] }),
  });

  // What cortex has actually changed — proof the self-improvement loops take
  // effect, not just emit proposals (learned-router leaderboard + applied
  // tuning + evolved agent instructions).
  const { data: effects } = useQuery({
    queryKey: ["cortex-effects"],
    queryFn: queryFn("/cortex/effects"),
  });
  const leaderboard = effects?.leaderboard || {};
  const tuning = effects?.tuning || {};
  const prompts = effects?.prompts || {};
  const skills = effects?.skills || {};
  const leaderRows = Object.entries(leaderboard).flatMap(([bucket, rows]) =>
    (rows || []).map((r) => ({ bucket, ...r }))
  );
  const tuningRows = Object.entries(tuning);
  const promptRows = Object.entries(prompts);
  const skillRows = Array.isArray(skills) ? skills : skills.items || [];
  const skillCount = Number(skills.count ?? skillRows.length);
  const hasEffects =
    leaderRows.length || tuningRows.length || promptRows.length || skillCount;

  const proposals = Array.isArray(data) ? data : data?.proposals || [];
  const q = search.trim().toLowerCase();
  const matchText = (value) =>
    !q || String(value || "").toLowerCase().includes(q);
  const rowMatches = (row) =>
    !q || matchText(JSON.stringify(row || {}));
  const filteredProposals = useMemo(
    () => proposals.filter((p) => rowMatches(p)),
    [proposals, q],
  );
  const filteredLeaderRows = useMemo(
    () => leaderRows.filter((r) => rowMatches(r)),
    [leaderRows, q],
  );
  const filteredSkillRows = useMemo(
    () => skillRows.filter((r) => rowMatches(r)),
    [skillRows, q],
  );
  const filteredTuningRows = useMemo(
    () => tuningRows.filter((r) => rowMatches(r)),
    [tuningRows, q],
  );
  const filteredPromptRows = useMemo(
    () => promptRows.filter((r) => rowMatches(r)),
    [promptRows, q],
  );

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Autonomy Inbox"
        title="Cortex"
        sub="Self-evolution proposals awaiting your decision. The swarm wants to reshape itself."
        actions={
          <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
            <input
              className="field w-full sm:w-56"
              aria-label="Search Cortex"
              placeholder="search proposals, skills, models..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <span className="badge border-hairline text-ash">
              open · <span className="ml-1 text-ember">{filteredProposals.length}</span>
            </span>
            <button
              onClick={() => scout.mutate()}
              disabled={scout.isPending}
              className="btn-ember disabled:opacity-50"
              title="Scout GitHub now for repos to ingest (files gated proposals)"
            >
              {scout.isPending ? "Scouting…" : "Scout now"}
            </button>
            <button
              onClick={() => clear.mutate("resolved")}
              disabled={clear.isPending}
              className="btn-ghost disabled:opacity-50"
              title="Drop already-decided / duplicate proposals from the cache"
            >
              Clear resolved
            </button>
            {confirmClearAll ? (
              <>
                <button
                  onClick={() => {
                    clear.mutate("all");
                    setConfirmClearAll(false);
                  }}
                  disabled={clear.isPending}
                  className="btn-ember"
                >
                  Confirm dismiss
                </button>
                <button
                  onClick={() => setConfirmClearAll(false)}
                  disabled={clear.isPending}
                  className="btn-ghost"
                >
                  Cancel
                </button>
              </>
            ) : (
              <button
                onClick={() => setConfirmClearAll(true)}
                disabled={clear.isPending}
                className="btn-ghost"
                title="Dismiss every cached proposal, including pending proposals"
              >
                Dismiss all
              </button>
            )}
          </div>
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
              {decide.isPending
                ? "deciding…"
                : q
                  ? `${filteredProposals.length}/${proposals.length} match`
                  : `${proposals.length} open`}
            </span>
          }
        />
        {isLoading ? (
          <Empty icon="≋">Listening to Cortex…</Empty>
        ) : filteredProposals.length === 0 ? (
          <Empty icon="◇">
            {q ? "No Cortex rows match that search." : "No open proposals. The swarm is content for now."}
          </Empty>
        ) : (
          <div className="divide-y divide-hairline/60">
            {filteredProposals.map((p) => {
              const id = p.id ?? p.proposal_id;
              return (
                <div key={id} className="px-4 py-4">
                  <div className="flex flex-col items-start justify-between gap-4 sm:flex-row">
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
                    <div className="flex flex-wrap gap-2 sm:shrink-0">
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

      <Panel className="mt-6">
        <PanelHead
          label="What Cortex changed"
          right={
            <span className="font-mono text-[11px] text-ash">
              effects · live
            </span>
          }
        />
        {!hasEffects ? (
          <Empty icon="◇">
            No effects yet. Cortex feeds the learned router, reusable skills,
            tuning, and agent instructions as builds run.
          </Empty>
        ) : (
          <div className="grid grid-cols-1 gap-6 px-4 py-4 md:grid-cols-4">
            <div>
              <div className="mb-2 font-mono text-[11px] uppercase text-ash">
                Learned router
              </div>
              {filteredLeaderRows.length === 0 ? (
                <p className="text-sm text-ash">No tournament data yet.</p>
              ) : (
                <ul className="space-y-1 text-sm text-bone">
                  {filteredLeaderRows.slice(0, 8).map((r, i) => (
                    <li key={i} className="flex justify-between gap-2">
                      <span className="truncate font-mono text-xs">
                        {r.bucket} · {r.model}
                      </span>
                      <span className="shrink-0 text-ash">
                        {Math.round((r.win_rate ?? 0) * 100)}% · {r.plays}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <div className="mb-2 font-mono text-[11px] uppercase text-ash">
                Reusable skills
              </div>
              {filteredSkillRows.length === 0 ? (
                <p className="text-sm text-ash">No learned skills yet.</p>
              ) : (
                <ul className="space-y-2 text-sm text-bone">
                  {filteredSkillRows.slice(0, 8).map((skill) => (
                    <li key={skill.slug || skill.title}>
                      <div className="flex justify-between gap-2">
                        <span className="truncate font-mono text-xs">
                          {skill.title || skill.slug}
                        </span>
                        <span className="shrink-0 text-ash">
                          {Math.round((skill.score ?? 0) * 100)}%
                        </span>
                      </div>
                      <p className="mt-1 truncate font-sans text-xs text-ash">
                        {skill.stack || "generic"} · {skill.source || "learned"}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <div className="mb-2 font-mono text-[11px] uppercase text-ash">
                Applied tuning
              </div>
              {filteredTuningRows.length === 0 ? (
                <p className="text-sm text-ash">No tuning overrides.</p>
              ) : (
                <ul className="space-y-1 text-sm text-bone">
                  {filteredTuningRows.map(([k, v]) => (
                    <li key={k} className="flex justify-between gap-2">
                      <span className="font-mono text-xs">{k}</span>
                      <span className="text-ash">{String(v)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            <div>
              <div className="mb-2 font-mono text-[11px] uppercase text-ash">
                Evolved instructions
              </div>
              {filteredPromptRows.length === 0 ? (
                <p className="text-sm text-ash">No prompt overrides.</p>
              ) : (
                <ul className="space-y-2 text-sm text-bone">
                  {filteredPromptRows.map(([agent, instr]) => (
                    <li key={agent}>
                      <Pill tone="ash">{agent}</Pill>
                      <p className="mt-1 max-w-xs truncate font-sans text-xs text-ash">
                        {instr}
                      </p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}
