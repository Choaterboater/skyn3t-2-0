import React, { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiPost, queryFn } from "../api.js";
import { isActiveBuild } from "../buildStatus.js";

// Approval-gated builds auto-reject when their gate times out (~60s by
// default), but Approve/Reject used to render only inside the Foundry's
// Recent builds table — an operator on any other route lost the race without
// ever seeing the gate. This app-level banner surfaces every approval_pending
// build on every route, with the decision inline against the same
// /studio/approve endpoint the Foundry table uses.
function pendingRefetchInterval(query) {
  const data = query?.state?.data;
  const rows = Array.isArray(data) ? data : data?.builds || [];
  return rows.some((build) => isActiveBuild(build?.status)) ? 5000 : false;
}

export default function PendingApprovalsBanner({ stream }) {
  const qc = useQueryClient();
  const builds = useQuery({
    queryKey: ["builds"],
    queryFn: queryFn("/builds"),
    refetchInterval: pendingRefetchInterval,
  });
  const decide = useMutation({
    mutationFn: ({ build_id, approved }) =>
      apiPost("/studio/approve", { build_id, approved, reason: "" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["builds"] }),
  });

  // Poll-bound discovery can burn most of the gate's timeout budget. Refetch
  // immediately when the shared stream announces a waiting gate (or a new
  // build that could carry one). Harmless no-op for streams that do not emit
  // approval.requested yet.
  const last = stream?.last;
  useEffect(() => {
    if (!last) return;
    if (last.type === "approval.requested" || last.type === "build.started") {
      qc.invalidateQueries({ queryKey: ["builds"] });
    }
  }, [last, qc]);

  const rows = Array.isArray(builds.data?.builds) ? builds.data.builds : [];
  const pending = rows.filter((build) => build?.approval_pending);
  if (pending.length === 0) return null;

  return (
    <div
      role="alert"
      className="mb-6 rounded-md border border-ember/50 bg-ember/10 px-4 py-3 font-mono text-[12px] leading-relaxed text-ember"
    >
      <div className="font-bold">
        {pending.length} build{pending.length > 1 ? "s" : ""} waiting on approval — an
        unanswered gate auto-rejects when it times out.
      </div>
      <ul className="mt-2 flex flex-col gap-2">
        {pending.map((build) => {
          const buildId = String(build.build_id || "");
          const stages = Array.isArray(build.approval_stages)
            ? build.approval_stages.filter(Boolean)
            : [];
          return (
            <li key={buildId} className="flex flex-wrap items-center gap-2">
              <span className="text-bone">{build.slug || buildId}</span>
              {stages.length ? (
                <span className="text-ember/80">
                  stage{stages.length > 1 ? "s" : ""} {stages.join(", ")}
                </span>
              ) : null}
              <button
                onClick={() => decide.mutate({ build_id: buildId, approved: true })}
                disabled={decide.isPending}
                className="btn-ember disabled:opacity-50"
                title="Approve the pending build gate"
              >
                Approve
              </button>
              <button
                onClick={() => decide.mutate({ build_id: buildId, approved: false })}
                disabled={decide.isPending}
                className="btn-ghost disabled:opacity-50"
                title="Reject the pending build gate"
              >
                Reject
              </button>
            </li>
          );
        })}
      </ul>
      {decide.isError ? (
        <div className="mt-2">{String(decide.error?.message || decide.error)}</div>
      ) : null}
    </div>
  );
}
