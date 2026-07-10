export function formatExampleUsd(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    return "price unknown";
  }
  if (value === 0) return "$0 (free)";
  if (value < 0.001) return `$${value.toFixed(6)}`;
  if (value < 1) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

function benchmarkDeltaLabel(alternative) {
  const delta = alternative?.benchmark_delta;
  if (typeof delta === "number" && Number.isFinite(delta)) {
    const sharedCount = Array.isArray(alternative?.shared_benchmark?.dimensions)
      ? alternative.shared_benchmark.dimensions.length
      : 0;
    return `benchmark ${Math.abs(delta).toFixed(1)} ${delta >= 0 ? "higher" : "lower"}${
      sharedCount ? ` across ${sharedCount} shared dimensions` : ""
    }`;
  }
  return alternative?.performance_label || "performance not compared";
}

export function formatModelOption(item) {
  if (!item || typeof item !== "object") return "price unknown";
  const cost = formatExampleUsd(item.example_cost_usd);
  const alternative = item.value_alternative;
  if (!alternative?.id) return `example ${cost}`;
  return `example ${cost} | value peer: ${alternative.id} ${formatExampleUsd(
    alternative.example_cost_usd
  )} (${alternative.savings_percent}% less, ${benchmarkDeltaLabel(alternative)})`;
}

export function findModelValue(items, modelId) {
  const normalized = String(modelId || "").trim();
  if (!normalized || !Array.isArray(items)) return null;
  return items.find((item) => String(item?.id || "") === normalized) || null;
}

export function describeModelValue(item, role = "model") {
  if (!item) return null;
  const price = formatExampleUsd(item.example_cost_usd);
  const band = item.relative_price_band === "high" ? "high catalog price" : null;
  const benchmark = item.benchmark?.score;
  const benchmarkProfile = String(item.benchmark?.profile || "app building").replaceAll("_", "-");
  const benchmarkText =
    typeof benchmark === "number" ? `${benchmarkProfile} benchmark ${benchmark.toFixed(1)}/100` : null;
  const head = [role, `example ${price}`, band, benchmarkText].filter(Boolean).join(" | ");
  const alternative = item.value_alternative;
  if (!alternative?.id) return head;
  const basis =
    alternative.comparison_basis === "task_benchmark"
      ? benchmarkDeltaLabel(alternative)
      : alternative.performance_label || "capability-only match; performance not compared";
  return `${head} | cheaper peer ${alternative.id} ${formatExampleUsd(
    alternative.example_cost_usd
  )} (${alternative.savings_percent}% less, ${basis})`;
}

export function describeExampleWorkload(workload) {
  if (!workload || typeof workload !== "object") {
    return "Example price per model call; actual build total depends on all calls and is not capped.";
  }
  const prompt = Number(workload.prompt_tokens || 0).toLocaleString("en-US");
  const completion = Number(workload.completion_tokens || 0).toLocaleString("en-US");
  return `Example price: ${prompt} input + ${completion} output tokens per call. ${
    workload.note || "Actual build total depends on all calls; this is not a cap."
  }`;
}
