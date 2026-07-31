const ACTIVE_STATUSES = new Set([
  "running",
  "queued",
  "queued_no_studio",
  "pending",
  "awaiting_approval",
  "building",
]);

const DELIVERED_STATUSES = new Set([
  "go",
  "completed",
  "completed_no_go",
  "applied",
]);

const SHIPPABLE_STATUSES = new Set(["go", "completed", "applied"]);
const PROBLEM_STATUSES = new Set([
  "cancelled",
  "failed",
  "rejected",
  "interrupted",
  "no_go",
  "completed_no_go",
]);

function normalized(value) {
  return String(value || "").trim().toLowerCase();
}

function readable(value) {
  return String(value || "unknown").replaceAll("_", " ");
}

function positiveFileCount(value) {
  const count = Number(value);
  return Number.isFinite(count) && count > 0 ? Math.floor(count) : 0;
}

// First gate_findings entry that actually blocked the verdict, when the row
// carries the ledger (quality_scorecard.gate_findings). Defensive: any
// missing/odd shape yields null rather than breaking an outcome pill.
function firstBlockedGateFinding(build) {
  const scorecard = build?.quality_scorecard;
  const findings = Array.isArray(scorecard?.gate_findings)
    ? scorecard.gate_findings
    : [];
  for (const finding of findings) {
    if (!finding || typeof finding !== "object" || finding.blocked !== true) continue;
    const gate = String(finding.gate || "").trim();
    if (!gate) continue;
    return { gate, reason: String(finding.reason || "").trim() };
  }
  return null;
}

export function buildOutcome(build = {}) {
  const status = normalized(build.status);
  const buildStatus = normalized(build.build_status) || status;
  const verdict = normalized(build.verdict);
  const explicitDelivery = normalized(build.delivery_state);
  const active = Boolean(
    build.build_active === true ||
    explicitDelivery === "building" ||
    ACTIVE_STATUSES.has(status) ||
    ACTIVE_STATUSES.has(buildStatus)
  );
  const explicitlyIncomplete = Boolean(
    build.is_complete === false ||
    explicitDelivery === "incomplete" ||
    explicitDelivery === "building"
  );
  const delivered = Boolean(
    !active &&
    !explicitlyIncomplete &&
    (
      build.is_complete === true ||
      explicitDelivery === "delivered" ||
      DELIVERED_STATUSES.has(status)
    )
  );
  const noGo = Boolean(
    verdict === "no_go" ||
    status === "no_go" ||
    status === "completed_no_go"
  );
  const releaseBlocked = Boolean(noGo || (verdict && verdict !== "go"));
  const shippable = Boolean(
    delivered &&
    !releaseBlocked &&
    (verdict === "go" || SHIPPABLE_STATUSES.has(status))
  );
  const cancelled = status === "cancelled" || buildStatus === "cancelled";
  const failed = status === "failed" || buildStatus === "failed";
  const rawLabel = readable(status || buildStatus || (delivered ? "delivered" : "incomplete"));
  const files = positiveFileCount(build.file_count);
  const partialDetail = files
    ? ` · ${files} partial file${files === 1 ? "" : "s"}`
    : "";

  let label = rawLabel;
  let detail = delivered ? "delivered" : `not delivered${partialDetail}`;
  let tone = PROBLEM_STATUSES.has(status) ? "ember" : "ash";
  let title = `Build status: ${rawLabel}. Delivery: ${delivered ? "delivered" : active ? "in progress" : "not delivered"}.`;

  if (active) {
    label = status === "building" ? "building" : rawLabel;
    detail = "in progress · not delivered yet";
    tone = "synapse";
    title = `Build status: ${rawLabel}. The build is still in progress and has not delivered an artifact yet.`;
  } else if (shippable) {
    label = "GO";
    detail = `delivered · ${rawLabel}`;
    tone = "plasma";
    title = `Build status: ${rawLabel}. Delivery: delivered. Release verdict: GO.`;
  } else if (delivered && releaseBlocked) {
    label = noGo ? "NO GO" : readable(verdict);
    detail = "delivered · not shippable";
    tone = "ember";
    title = `Build status: ${rawLabel}. An artifact was delivered, but its release verdict is ${readable(verdict || "no_go").toUpperCase()}.`;
    // Name the blocking gate when the ledger is on the row — a bare NO GO
    // gives the operator nothing to fix before the rebuild.
    const blocking = firstBlockedGateFinding(build);
    if (blocking) {
      title += ` Blocked by ${blocking.gate}${blocking.reason ? `: ${blocking.reason}` : ""}.`;
    }
  } else if (cancelled) {
    label = "cancelled";
    detail = delivered ? "delivered artifact · cancelled run" : `not delivered${partialDetail}`;
    tone = "ember";
    title = delivered
      ? "The run was cancelled, but the API reports a delivered artifact. It is not treated as shippable."
      : "The build was cancelled before a completed artifact was delivered. Partial files may remain.";
  } else if (failed) {
    label = "failed";
    detail = delivered ? "delivered artifact · failed run" : `not delivered${partialDetail}`;
    tone = "ember";
    title = delivered
      ? "The run failed, but the API reports a delivered artifact. It is not treated as shippable."
      : "The build failed before a completed artifact was delivered. Partial files may remain.";
  } else if (!delivered) {
    label = rawLabel === "unknown" ? "incomplete" : rawLabel;
    tone = PROBLEM_STATUSES.has(status) ? "ember" : "ash";
    title = `Build status: ${rawLabel}. No completed delivery is recorded. Partial files may remain.`;
  }

  return {
    status,
    buildStatus,
    verdict,
    deliveryState: active ? "building" : delivered ? "delivered" : "incomplete",
    active,
    delivered,
    shippable,
    cancelled,
    failed,
    noGo,
    label,
    detail,
    tone,
    title,
  };
}

export function summarizeBuildOutcomes(builds = []) {
  const rows = Array.isArray(builds) ? builds : [];
  const outcomes = rows.map((build) => buildOutcome(build));
  const recordedWasteUsd = rows.reduce((sum, build) => {
    const value = Number(build?.wasted_usd);
    return Number.isFinite(value) && value > 0 ? sum + value : sum;
  }, 0);
  const nonShippableSpendUsd = rows.reduce((sum, build, index) => {
    const outcome = outcomes[index];
    if (outcome.active || outcome.shippable) return sum;
    const truth = build?.cost_truth || {};
    const raw = truth.llm_cost_known === false
      ? truth.llm_known_cost_usd
      : build?.non_shippable_spend_usd;
    const value = Number(raw);
    return Number.isFinite(value) && value > 0 ? sum + value : sum;
  }, 0);
  const unknownCostRows = rows.filter((build) => {
    const outcome = buildOutcome(build);
    return !outcome.active && !outcome.shippable &&
      build?.cost_truth?.llm_cost_known === false;
  }).length;

  return {
    total: outcomes.length,
    active: outcomes.filter((outcome) => outcome.active).length,
    delivered: outcomes.filter((outcome) => outcome.delivered).length,
    shippable: outcomes.filter((outcome) => outcome.shippable).length,
    notDelivered: outcomes.filter((outcome) => !outcome.delivered).length,
    cancelled: outcomes.filter((outcome) => outcome.cancelled).length,
    failed: outcomes.filter((outcome) => outcome.failed).length,
    recordedWasteUsd: Number(recordedWasteUsd.toFixed(6)),
    nonShippableSpendUsd: Number(nonShippableSpendUsd.toFixed(6)),
    unknownCostRows,
    nonShippableSpendIsLowerBound: unknownCostRows > 0,
  };
}
