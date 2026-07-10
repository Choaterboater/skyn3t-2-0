function objectValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function finiteNumber(value) {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function firstText(...values) {
  for (const value of values) {
    const text = String(value || "").trim();
    if (text) return text;
  }
  return "";
}

function knownCount(...values) {
  const counts = values
    .map(finiteNumber)
    .filter((value) => value != null && value >= 0);
  return counts.length ? Math.max(...counts) : null;
}

function longestArray(...values) {
  return values
    .filter(Array.isArray)
    .reduce((longest, current) => current.length > longest.length ? current : longest, []);
}

export function formatMetadataUsd(value) {
  const parsed = finiteNumber(value);
  return parsed == null ? "—" : `$${parsed.toFixed(4)}`;
}

export function projectBuildMetadata(project = {}) {
  const trace = objectValue(project.model_trace);
  const scorecard = objectValue(project.quality_scorecard);
  const costTruth = objectValue(project.cost_truth || scorecard.cost_truth);
  const stageSkills = objectValue(project.stage_skills_used || trace.stage_skills_used);
  const skills = longestArray(project.skills_used, trace.skills_used);
  const recall = longestArray(project.recall_used, trace.recall_used);
  const stages = longestArray(project.stages, trace.stages);
  const stageCosts = longestArray(project.stage_costs, trace.stage_costs);
  const roleStages = Object.values(stageSkills).filter(
    (items) => Array.isArray(items) && items.length > 0,
  ).length;

  const promptCount = knownCount(trace.prompt_count, project.prompt_count);
  const stageCount = knownCount(
    Array.isArray(trace.stages) ? trace.stages.length : null,
    project.stage_count,
    Array.isArray(project.stages) ? project.stages.length : null,
    Array.isArray(trace.stage_costs) ? trace.stage_costs.length : null,
    Array.isArray(project.stage_costs) ? project.stage_costs.length : null,
  );
  const skillCount = knownCount(
    Array.isArray(project.skills_used) ? project.skills_used.length : null,
    Array.isArray(trace.skills_used) ? trace.skills_used.length : null,
    scorecard.skills_count,
  );
  const recallCount = knownCount(
    Array.isArray(project.recall_used) ? project.recall_used.length : null,
    Array.isArray(trace.recall_used) ? trace.recall_used.length : null,
    scorecard.recall_count,
  );

  let stageCostTotal = 0;
  let stageCostCount = 0;
  for (const item of stageCosts) {
    const row = objectValue(item);
    const cost = finiteNumber(row.cost_usd ?? row.cost);
    if (cost == null) continue;
    stageCostTotal += cost;
    stageCostCount += 1;
  }
  const runCost = finiteNumber(
    costTruth.llm_cost_usd ?? project.cost_usd ?? scorecard.cost_usd,
  );
  const modelOverride = firstText(trace.model_override, project.model_override);
  const codegenModel = firstText(
    trace.codegen_model,
    project.codegen_model,
    trace.effective_codegen_model,
  );
  const profile = firstText(project.build_profile, trace.profile) || "unknown profile";
  const backend = firstText(trace.backend, project.llm_backend, project.backend) || "unknown backend";
  const model = codegenModel || modelOverride || "auto model";
  const modelSource = codegenModel
    ? modelOverride && codegenModel === modelOverride
      ? "codegen override"
      : "codegen"
    : modelOverride
      ? "codegen override"
      : "auto route";
  const runCostLabel = formatMetadataUsd(runCost);
  const stageCostLabel = stageCostCount
    ? `${formatMetadataUsd(stageCostTotal)} / ${stageCostCount}`
    : "—";
  const countLabel = (value) => value == null ? "—" : String(value);
  const title = [
    `profile: ${profile}`,
    `backend: ${backend}`,
    `model: ${modelSource} · ${model}`,
    `prompts: ${countLabel(promptCount)}`,
    `stages: ${countLabel(stageCount)}`,
    `run cost: ${runCostLabel}`,
    `stage cost: ${stageCostLabel}`,
    `skills: ${countLabel(skillCount)}`,
    `recall: ${countLabel(recallCount)}`,
    `stage roles: ${roleStages}`,
  ].join(" · ");

  return {
    profile,
    backend,
    model,
    codegenModel,
    modelOverride,
    modelSource,
    skills,
    recall,
    roleStages,
    promptCount,
    stageCount,
    skillCount,
    recallCount,
    runCost,
    runCostLabel,
    stageCostCount,
    stageCostTotal,
    stageCostLabel,
    title,
  };
}
