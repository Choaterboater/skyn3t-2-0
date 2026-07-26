import React, { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill, Empty } from "../components/ui.jsx";

export default function Cortex() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [candidateGoal, setCandidateGoal] = useState("");
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

  const { data: candidateData, error: candidateError } = useQuery({
    queryKey: ["cortex-candidates"],
    queryFn: queryFn("/cortex/candidates"),
  });
  const candidateReports = candidateData?.reports || [];
  const runCandidate = useMutation({
    mutationFn: (goal) => apiPost("/cortex/candidates", { goal }),
    onSuccess: () => {
      setCandidateGoal("");
      qc.invalidateQueries({ queryKey: ["cortex-candidates"] });
    },
  });
  const candidatePolicy = useMutation({
    mutationFn: (body) => apiPost("/settings/cortex_candidates", body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cortex-candidates"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
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

      <Panel className="mb-6">
        <PanelHead
          label="Verified code candidates"
          right={
            <span className="font-mono text-[11px] text-ash">
              isolated worktree · local only
            </span>
          }
        />
        <div className="grid gap-5 p-4 lg:grid-cols-[minmax(0,1.1fr)_minmax(0,0.9fr)]">
          <div>
            <textarea
              aria-label="Cortex code candidate goal"
              className="field min-h-24 w-full"
              value={candidateGoal}
              onChange={(event) => setCandidateGoal(event.target.value)}
              placeholder="Describe a focused improvement to Studio, Cortex, orchestration, generated-app templates, or product UI…"
            />
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <button
                className="btn-ember"
                disabled={
                  runCandidate.isPending ||
                  !candidateGoal.trim() ||
                  candidateData?.enabled === false
                }
                onClick={() => runCandidate.mutate(candidateGoal.trim())}
              >
                {runCandidate.isPending ? "Building + proving…" : "Run candidate"}
              </button>
              <span className="font-mono text-[10px] text-ash">
                exact GUI route · Codex CLI or OpenRouter · full Python + dashboard gates
              </span>
            </div>
            {runCandidate.error ? (
              <p className="mt-2 font-mono text-[11px] text-ember">
                {runCandidate.error.message}
              </p>
            ) : null}
            {runCandidate.data ? (
              <p className="mt-2 font-mono text-[11px] text-plasma">
                {runCandidate.data.candidate?.status} ·{" "}
                {runCandidate.data.routing?.effective_backend || "selected route"} ·{" "}
                no remote push
              </p>
            ) : null}
          </div>
          <div className="space-y-3 rounded border border-hairline bg-void/40 p-3">
            <label className="flex items-start justify-between gap-4">
              <span>
                <span className="block text-sm text-bone">Code candidates</span>
                <span className="block text-xs text-ash">
                  Uses the selected Codex CLI or OpenRouter route; other local CLIs are rejected before a worktree is created. Strict product-only paths; APIs, dependencies, CI, secrets, and deploy stay blocked.
                </span>
              </span>
              <input
                type="checkbox"
                aria-label="Enable Cortex code candidates"
                checked={candidateData?.enabled !== false}
                disabled={candidatePolicy.isPending}
                onChange={(event) =>
                  candidatePolicy.mutate({
                    enabled: event.target.checked,
                    auto_merge: Boolean(candidateData?.auto_merge),
                    merge_strategy: candidateData?.merge_strategy || "ff-only",
                  })
                }
              />
            </label>
            <label className="flex items-start justify-between gap-4 border-t border-hairline pt-3">
              <span>
                <span className="block text-sm text-bone">Auto-merge local main</span>
                <span className="block text-xs text-ash">
                  Only after every gate passes. Never pushes, deploys, or publishes.
                </span>
              </span>
              <input
                type="checkbox"
                aria-label="Auto-merge verified Cortex candidates"
                checked={Boolean(candidateData?.auto_merge)}
                disabled={candidatePolicy.isPending || candidateData?.enabled === false}
                onChange={(event) =>
                  candidatePolicy.mutate({
                    enabled: candidateData?.enabled !== false,
                    auto_merge: event.target.checked,
                    merge_strategy: candidateData?.merge_strategy || "ff-only",
                  })
                }
              />
            </label>
          </div>
        </div>
        {candidateError ? (
          <p className="px-4 pb-3 font-mono text-[11px] text-ember">
            Candidate history unavailable: {candidateError.message}
          </p>
        ) : candidateReports.length ? (
          <div className="divide-y divide-hairline/60 border-t border-hairline">
            {candidateReports.slice(0, 8).map((report) => (
              <div
                key={report.candidate_id}
                className="grid gap-2 px-4 py-3 md:grid-cols-[10rem_minmax(0,1fr)_auto]"
              >
                <div>
                  <Pill tone={report.status === "merged" ? "plasma" : "ash"}>
                    {report.status}
                  </Pill>
                  <div className="mt-1 font-mono text-[10px] text-ash">
                    {report.candidate_id.slice(0, 10)}
                  </div>
                </div>
                <div className="min-w-0">
                  <div className="truncate font-mono text-[11px] text-bone">
                    {(report.changed_paths || []).join(", ") || "no changed paths"}
                  </div>
                  {(report.errors || []).length ? (
                    <div className="mt-1 text-xs text-ember">
                      {report.errors[0]}
                    </div>
                  ) : null}
                </div>
                <div className="font-mono text-[10px] text-ash">
                  {(report.commands || []).filter((row) => row.phase === "verification" && row.passed).length}
                  {" "}gates passed
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="border-t border-hairline">
            <Empty icon="◇">No code candidates have run yet.</Empty>
          </div>
        )}
      </Panel>

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
