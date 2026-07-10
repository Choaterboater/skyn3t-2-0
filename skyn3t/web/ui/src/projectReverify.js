const REVERIFYABLE_TERMINAL_STATUSES = new Set([
  "cancelled",
  "failed",
  "incomplete",
]);

const ACTIVE_STATUSES = new Set([
  "running",
  "queued",
  "queued_no_studio",
  "pending",
  "awaiting_approval",
  "building",
]);

function normalized(value) {
  return String(value || "").trim().toLowerCase();
}

export function canReverifyLocally(project = {}) {
  if (typeof project.can_reverify === "boolean") {
    return project.can_reverify;
  }

  const fileCount = Number(project.file_count);
  const statuses = [project.status, project.build_status].map(normalized);
  const active = project.build_active === true ||
    normalized(project.delivery_state) === "building" ||
    statuses.some((status) => ACTIVE_STATUSES.has(status));
  const terminal = statuses.some((status) =>
    REVERIFYABLE_TERMINAL_STATUSES.has(status)
  );

  return Boolean(
    terminal &&
      project.has_manifest === true &&
      Number.isFinite(fileCount) &&
      fileCount > 0 &&
      !active &&
      project.is_complete === false
  );
}

export function describeLocalReverify(result = {}) {
  const reportedCalls = Number(result.skyn3t_model_invocations);
  const modelCalls = Number.isFinite(reportedCalls) && reportedCalls >= 0
    ? Math.floor(reportedCalls)
    : 0;
  const score = result.score == null || result.score === ""
    ? null
    : Number(result.score);
  const scoreText = Number.isFinite(score) ? ` · score ${score}` : "";
  const proofPassed = result.proof?.passed === true;
  const outcome = result.promoted === true
    ? "promoted after local proof"
    : proofPassed
      ? "local proof passed · not promoted"
      : "local proof did not pass";
  const reason = !result.promoted && result.reason
    ? ` · ${String(result.reason)}`
    : "";
  const callsText = `${modelCalls} SkyN3t model call${modelCalls === 1 ? "" : "s"}`;
  const externalText = result.execution?.external_requests_observed == null
    ? " · external script requests/cost unknown"
    : "";

  return `${outcome}${scoreText}${reason} · ${callsText}${externalText}`;
}
