export function describeCostTruth(project = {}) {
  const truth = project.cost_truth;
  if (!truth || typeof truth !== "object" || !Object.keys(truth).length) {
    return {
      label: "",
      classification: "unknown",
      externalUnknown: false,
      llmCostKnown: null,
      knownCostUsd: null,
      amountLabel: "",
      title: "",
    };
  }

  const classification = String(truth.llm_cost_classification || "unknown");
  const label =
    classification === "provider_confirmed"
      ? "provider-confirmed"
      : classification === "estimate"
        ? "estimate"
        : classification === "mixed"
          ? "mixed"
          : classification === "partial"
            ? "partial - CLI unknown"
            : "CLI cost unknown";
  const llmCostKnown = truth.llm_cost_known !== false;
  const rawKnownCost = llmCostKnown
    ? truth.llm_cost_usd ?? project.cost_usd
    : truth.llm_known_cost_usd;
  const knownCost = Number(rawKnownCost);
  const knownCostUsd = Number.isFinite(knownCost) && knownCost >= 0 ? knownCost : null;
  const amountLabel = llmCostKnown
    ? knownCostUsd == null
      ? "unknown"
      : `$${knownCostUsd.toFixed(4)}`
    : knownCostUsd != null && knownCostUsd > 0
      ? `>= $${knownCostUsd.toFixed(4)}`
      : "unknown";
  const external =
    truth.external_asset_usage && typeof truth.external_asset_usage === "object"
      ? truth.external_asset_usage
      : {};
  const predictionCount = Math.max(0, Number(external.prediction_count || 0));
  const attemptCount = Math.max(0, Number(external.attempt_count || 0));
  const historicalCount = Math.max(
    0,
    Number(external.historical_generated_asset_count || 0),
  );
  const externalUnknown =
    Math.max(predictionCount, attemptCount, historicalCount) > 0 &&
    external.dollar_cost_known !== true;
  const notes = [String(truth.llm_cost_label || label)];
  if (externalUnknown) {
    if (predictionCount > 0) {
      const attempts = attemptCount || predictionCount;
      notes.push(
        `${predictionCount} provider-evidenced Replicate prediction${predictionCount === 1 ? "" : "s"} across ${attempts} attempt${attempts === 1 ? "" : "s"}; exact asset dollars unavailable`,
      );
    } else if (attemptCount > 0) {
      notes.push(
        `${attemptCount} Replicate attempt${attemptCount === 1 ? "" : "s"}; exact asset dollars unavailable`,
      );
    } else {
      notes.push(
        `${historicalCount} historical generated asset${historicalCount === 1 ? "" : "s"}; provider attempt and dollar evidence unavailable`,
      );
    }
  }
  if (Number(truth.max_unconfirmed_exposure_usd || 0) > 0) {
    notes.push(
      `up to $${Number(truth.max_unconfirmed_exposure_usd).toFixed(4)} unconfirmed exposure`,
    );
  }
  return {
    label,
    classification,
    externalUnknown,
    llmCostKnown,
    knownCostUsd,
    amountLabel,
    title: notes.join("; "),
  };
}
