import React, { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill, Empty } from "../components/ui.jsx";

export default function Cortex() {
  const [reviewNotes, setReviewNotes] = useState({});
  const [followUpBriefs, setFollowUpBriefs] = useState({});
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [candidateGoal, setCandidateGoal] = useState("");
  const [confirmClearAll, setConfirmClearAll] = useState(false);
  const [rerunNodes, setRerunNodes] = useState({});
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
  const { data: autopilotData, error: autopilotError } = useQuery({
    queryKey: ["cortex-autopilot"],
    queryFn: queryFn("/cortex/autopilot"),
  });
  const setAutopilot = useMutation({
    mutationFn: (enabled) => apiPost("/cortex/autopilot", { enabled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cortex-autopilot"] });
      qc.invalidateQueries({ queryKey: ["cortex-candidates"] });
    },
  });  const candidateReports = candidateData?.reports || [];
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

  const { data: graphData, isLoading: graphLoading, error: graphError } = useQuery({
    queryKey: ["cortex-graphs"],
    queryFn: queryFn("/cortex/graphs?limit=8"),
  });
  const graphRuns = graphData?.runs || [];
  const rerunGraph = useMutation({
    mutationFn: ({ runId, fromNodeId }) =>
      apiPost(`/cortex/graphs/${encodeURIComponent(runId)}/rerun`, {
        from_node_id: fromNodeId,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cortex-graphs"] }),
  });

  const { data: graphReviewData, isLoading: graphReviewLoading, error: graphReviewError } = useQuery({
    queryKey: ["cortex-graph-reviews"],
    queryFn: queryFn("/cortex/graph-reviews?limit=12"),
  });
  const graphReviews = graphReviewData?.reviews || [];
  const decideGraphReview = useMutation({
    mutationFn: ({ comparisonId, decision, note }) =>
      apiPost(`/cortex/graph-reviews/${encodeURIComponent(comparisonId)}/decide`, {
        decision,
        note,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cortex-graph-reviews"] });
      qc.invalidateQueries({ queryKey: ["cortex-graphs"] });
    },
  });
  const queueGraphReviewBuild = useMutation({
    mutationFn: ({ comparisonId, brief }) =>
      apiPost(`/cortex/graph-reviews/${encodeURIComponent(comparisonId)}/build`, { brief }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cortex-graph-reviews"] }),
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
        eyebrow="Cortex · Local improvement"
        title="Cortex"
        sub="See what SkyN3t is fixing, learning, and trying next. Technical pipeline details stay optional."
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

      <Panel className="mb-6">
        <PanelHead
          label="Autopilot"
          right={<span className="font-mono text-[11px] text-ash">local only · no remote push</span>}
        />
        <div className="grid gap-4 p-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-center">
          <div>
            <p className="text-sm text-bone">
              {autopilotData?.enabled ? "SkyN3t can automatically work through local repairs and learning experiments." : "Autopilot is off. Turn it on when you want SkyN3t to repair and learn locally without waiting for each click."}
            </p>
            <p className="mt-1 text-xs text-ash">
              {autopilotData?.open_incidents?.length || 0} problems waiting · {autopilotData?.recent_runs?.length || 0} recent runs · generated apps and SkyN3t only
            </p>
            {autopilotData?.active ? <p className="mt-2 font-mono text-[11px] text-plasma">Working now: {autopilotData.active.summary}</p> : null}
            {autopilotError ? <p className="mt-2 font-mono text-[11px] text-ember">Autopilot status unavailable: {autopilotError.message}</p> : null}
          </div>
          <button
            className={autopilotData?.enabled ? "btn-ghost" : "btn-ember"}
            disabled={setAutopilot.isPending}
            onClick={() => setAutopilot.mutate(!autopilotData?.enabled)}
          >
            {setAutopilot.isPending ? "Updating…" : autopilotData?.enabled ? "Stop autopilot" : "Start autopilot"}
          </button>
        </div>
      </Panel>
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

      <Panel className="mb-6">
        <PanelHead
          label="Build preparation details"
          right={
            <span className="font-mono text-[11px] text-ash">
              technical history · local evidence
            </span>
          }
        />
        <div className="border-b border-hairline bg-void/35 px-4 py-3 text-sm text-ash">
          SkyN3t first checks that it understands the app, that build tools are ready, and that useful examples were researched. These are technical details; a retry only rechecks the selected step.
        </div>
        {graphError ? (
          <p className="px-4 py-3 font-mono text-[11px] text-ember">
            Graph history unavailable: {graphError.message}
          </p>
        ) : graphLoading ? (
          <Empty icon="≋">Loading durable graph evidence…</Empty>
        ) : graphData?.available === false ? (
          <Empty icon="◇">Graph history is unavailable right now.</Empty>
        ) : graphRuns.length === 0 ? (
          <Empty icon="◇">
            No completed build preflights yet. The next Studio build will appear here.
          </Empty>
        ) : (
          <div className="divide-y divide-hairline/60">
            {graphRuns.map((run) => {
              const rerunnableNodes = run.rerunnable_nodes || [];
              const selectedNode = rerunNodes[run.run_id] || rerunnableNodes[0] || "";
              const comparison = run.comparison;
              return (
                <div key={run.run_id} className="px-4 py-4">
                  <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_18rem]">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <Pill tone={run.status === "succeeded" ? "plasma" : "ash"}>
                          {run.status}
                        </Pill>
                        {run.rerun ? <Pill tone="ash">forked evidence</Pill> : null}
                        <span className="font-mono text-[10px] text-ash">
                          {run.graph_id} · v{run.graph_version}
                        </span>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-bone">
                        <span>{run.build?.slug || run.build?.build_id || "preflight"}</span>
                        {run.build?.stack ? <span className="text-ash">{run.build.stack}</span> : null}
                        <span className="text-ash">{run.run_id}</span>
                      </div>
                      <div className="mt-3 flex flex-wrap gap-2">
                        {Object.entries(run.nodes || {}).map(([node, status]) => (
                          <span key={node} className="badge border-hairline text-ash">
                            {({ product_contract: "App requirements understood", toolchain: "Build tools ready", similarity_research: "Examples researched" }[node] || node)} · <span className="text-bone">{status}</span>
                          </span>
                        ))}
                      </div>
                      {comparison ? (
                        <div className="mt-3 rounded border border-hairline bg-void/45 p-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <Pill tone={comparison.outcome === "equivalent" ? "plasma" : "ember"}>
                              {comparison.outcome}
                            </Pill>
                            <Pill tone="ash">{comparison.promotion_status}</Pill>
                            <span className="font-mono text-[10px] text-ash">
                              {comparison.rerun_nodes?.join(" → ")}
                            </span>
                          </div>
                          <p className="mt-2 font-mono text-[10px] text-ash">
                            immutable proof digests · {String(comparison.baseline_digest || "").slice(0, 12)} → {String(comparison.candidate_digest || "").slice(0, 12)}
                          </p>
                        </div>
                      ) : null}
                    </div>
                    <div className="rounded border border-hairline bg-void/40 p-3">
                      <label className="block font-mono text-[10px] uppercase text-ash">
                        Rerun from
                        <select
                          className="field mt-2 w-full"
                          aria-label={`Rerun node for ${run.run_id}`}
                          value={selectedNode}
                          disabled={!rerunnableNodes.length || rerunGraph.isPending}
                          onChange={(event) =>
                            setRerunNodes((current) => ({
                              ...current,
                              [run.run_id]: event.target.value,
                            }))
                          }
                        >
                          {rerunnableNodes.map((node) => (
                            <option key={node} value={node}>{node}</option>
                          ))}
                        </select>
                      </label>
                      <button
                        className="btn-ember mt-3 w-full disabled:opacity-50"
                        disabled={!selectedNode || rerunGraph.isPending}
                        onClick={() => rerunGraph.mutate({ runId: run.run_id, fromNodeId: selectedNode })}
                      >
                        {rerunGraph.isPending ? "Trying again…" : "Try this step again"}
                      </button>
                      <p className="mt-2 text-xs text-ash">
                        A new immutable run is created. Promotion still needs review.
                      </p>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {rerunGraph.error ? (
          <p className="border-t border-hairline px-4 py-3 font-mono text-[11px] text-ember">
            Rerun failed: {rerunGraph.error.message}
          </p>
        ) : null}
        {rerunGraph.data ? (
          <p className="border-t border-hairline px-4 py-3 font-mono text-[11px] text-plasma">
            Created {rerunGraph.data.graph?.run_id} · {rerunGraph.data.comparison?.outcome} · {rerunGraph.data.comparison?.promotion_status}
          </p>
        ) : null}
      </Panel>

      <Panel className="mb-6">
        <PanelHead
          label="Experiment review inbox"
          right={
            <span className="font-mono text-[11px] text-ash">
              {graphReviewData?.pending_count || 0} experiment receipts
            </span>
          }
        />
        <div className="border-b border-hairline bg-void/35 px-4 py-3 text-sm text-ash">
          These receipts show whether repeating a build-preparation step changed the result.
          They never directly change code, settings, policies, or skills; detailed actions stay
          available only when you need to inspect an experiment.
        </div>
        {graphReviewError ? (
          <p className="px-4 py-3 font-mono text-[11px] text-ember">
            Review inbox unavailable: {graphReviewError.message}
          </p>
        ) : graphReviewLoading ? (
          <Empty icon="≋">Loading immutable experiment receipts…</Empty>
        ) : graphReviewData?.available === false ? (
          <Empty icon="◇">Experiment review history is unavailable right now.</Empty>
        ) : graphReviews.length === 0 ? (
          <Empty icon="◇">No experiment receipts are waiting to be inspected.</Empty>
        ) : (
          <div className="divide-y divide-hairline/60">
            {graphReviews.map((review) => {
              const comparison = review.comparison || {};
              const decision = review.decision;
              const dispatch = review.build_dispatch;
              const comparisonId = comparison.comparison_id || "";
              const note = reviewNotes[comparisonId] || "";
              const followUpBrief = followUpBriefs[comparisonId] || "";
              return (
                <div key={comparisonId} className="px-4 py-4">
                  <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem]">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <Pill tone={comparison.outcome === "equivalent" ? "plasma" : "ember"}>
                          {comparison.outcome || "comparison"}
                        </Pill>
                        <Pill tone={decision ? (decision.decision === "keep" ? "plasma" : "ember") : "ash"}>
                          {decision ? decision.decision : "review required"}
                        </Pill>
                        <span className="font-mono text-[10px] text-ash">
                          {comparison.rerun_nodes?.join(" → ")}
                        </span>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-bone">
                        <span>{review.source_build?.slug || review.source_build?.build_id || "preflight"}</span>
                        {review.source_build?.stack ? <span className="text-ash">{review.source_build.stack}</span> : null}
                        <span className="text-ash">{comparison.comparison_id}</span>
                      </div>
                      <p className="mt-3 font-mono text-[10px] text-ash">
                        immutable proof digests · {String(comparison.baseline_digest || "").slice(0, 12)} → {String(comparison.candidate_digest || "").slice(0, 12)}
                      </p>
                      {decision ? (
                        <div className="mt-3 rounded border border-hairline bg-void/45 p-3">
                          <div className="flex flex-wrap items-center gap-2">
                            <Pill tone={decision.decision === "keep" ? "plasma" : "ember"}>{decision.decision}</Pill>
                            <span className="font-mono text-[10px] text-ash">
                              immutable receipt · {String(decision.decision_id || "").slice(0, 12)} · no promotion
                            </span>
                          </div>
                          <p className="mt-2 text-xs text-ash">
                            {decision.note || "No operator note recorded."}
                          </p>
                          {dispatch ? (
                            <p className="mt-2 font-mono text-[10px] text-plasma">
                              Normal Studio build queued · {dispatch.build_id}
                            </p>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                    <div className="rounded border border-hairline bg-void/40 p-3">
                      {!decision ? (
                        <>
                          <label className="block font-mono text-[10px] uppercase text-ash">
                            Decision note
                            <textarea
                              className="field mt-2 min-h-24 w-full"
                              aria-label={`Review note for ${comparisonId}`}
                              maxLength={2000}
                              value={note}
                              disabled={decideGraphReview.isPending}
                              onChange={(event) =>
                                setReviewNotes((current) => ({
                                  ...current,
                                  [comparisonId]: event.target.value,
                                }))
                              }
                              placeholder="Why should Cortex keep or reject this evidence?"
                            />
                          </label>
                          <div className="mt-3 grid grid-cols-2 gap-2">
                            <button
                              className="btn-ember disabled:opacity-50"
                              disabled={!comparisonId || decideGraphReview.isPending}
                              onClick={() => decideGraphReview.mutate({ comparisonId, decision: "keep", note })}
                            >
                              {decideGraphReview.isPending ? "Recording…" : "Keep evidence"}
                            </button>
                            <button
                              className="btn-ghost disabled:opacity-50"
                              disabled={!comparisonId || decideGraphReview.isPending}
                              onClick={() => decideGraphReview.mutate({ comparisonId, decision: "reject", note })}
                            >
                              Reject evidence
                            </button>
                          </div>
                        </>
                      ) : decision.decision === "keep" && !dispatch ? (
                        <>
                          <label className="block font-mono text-[10px] uppercase text-ash">
                            Optional follow-up build brief
                            <textarea
                              className="field mt-2 min-h-24 w-full"
                              aria-label={`Follow-up build brief for ${comparisonId}`}
                              maxLength={12000}
                              value={followUpBrief}
                              disabled={queueGraphReviewBuild.isPending}
                              onChange={(event) =>
                                setFollowUpBriefs((current) => ({
                                  ...current,
                                  [comparisonId]: event.target.value,
                                }))
                              }
                              placeholder="Describe the new normal Studio build you want to run."
                            />
                          </label>
                          <button
                            className="btn-ember mt-3 w-full disabled:opacity-50"
                            disabled={!comparisonId || !followUpBrief.trim() || queueGraphReviewBuild.isPending}
                            onClick={() => queueGraphReviewBuild.mutate({ comparisonId, brief: followUpBrief.trim() })}
                          >
                            {queueGraphReviewBuild.isPending ? "Queueing normal build…" : "Queue normal Studio build"}
                          </button>
                          <p className="mt-2 text-xs text-ash">
                            This uses the standard Studio build route and its existing safeguards.
                          </p>
                        </>
                      ) : (
                        <p className="text-xs text-ash">
                          {dispatch
                            ? "The explicit follow-up was already handed to the normal Studio queue."
                            : "Rejected evidence cannot queue a follow-up build."}
                        </p>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
        {decideGraphReview.error ? (
          <p className="border-t border-hairline px-4 py-3 font-mono text-[11px] text-ember">
            Decision was not recorded: {decideGraphReview.error.message}
          </p>
        ) : null}
        {queueGraphReviewBuild.error ? (
          <p className="border-t border-hairline px-4 py-3 font-mono text-[11px] text-ember">
            Follow-up build was not queued: {queueGraphReviewBuild.error.message}
          </p>
        ) : null}
        {queueGraphReviewBuild.data ? (
          <p className="border-t border-hairline px-4 py-3 font-mono text-[11px] text-plasma">
            Queued normal Studio build {queueGraphReviewBuild.data.build?.build_id}; the experiment remains review-only.
          </p>
        ) : null}
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
