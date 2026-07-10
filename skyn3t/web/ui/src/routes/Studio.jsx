import React, { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiFetch, apiPost } from "../api.js";
import {
  PageHeader,
  Panel,
  PanelHead,
  Stat,
  Pill,
  Empty,
  SignalGrid,
  ErrorText,
  verdictTone,
} from "../components/ui.jsx";
import {
  DebugTimeline,
  FilesSoFar,
  PreviewPanel,
  StageLedger,
  pipelineFromEvents,
} from "../components/cockpit.jsx";
import {
  describeExampleWorkload,
  describeModelValue,
  findModelValue,
  formatExampleUsd,
  formatModelOption,
} from "../modelValue.js";

// Fallback rail shown before the first `build.started` arrives. Once a build is
// live, the rail is driven off the REAL emitted plan (build.started.stages) — so
// it always matches the backend pipeline instead of this fixed guess.
const STAGES = [
  "brainstorm",
  "research",
  "architecture",
  "design",
  "codegen",
  "review",
  "verify_build",
  "package",
];

const ROUTING_PREVIEW_ESTIMATES = [
  { key: "prompt_1k_completion_1k", label: "small run", promptTokens: 1000, completionTokens: 1000 },
  { key: "prompt_5k_completion_2k", label: "standard run", promptTokens: 5000, completionTokens: 2000 },
  { key: "prompt_20k_completion_8k", label: "heavy run", promptTokens: 20000, completionTokens: 8000 },
];

const MODEL_CATALOG_PAGE_SIZE = 24;

const ROUTING_TIER_LABELS = {
  cheap: "economy tasks",
  ui: "ui",
  backend: "backend",
  strong: "strong",
  docs: "docs",
};

function routingPolicyHint(profile) {
  const normalized = String(profile || "cheap_learned");
  if (normalized === "fast") {
    return "Fast profile leans lower-latency routing over deep optimization.";
  }
  if (normalized === "balanced") {
    return "Balanced profile trades cost for quality on reasoning-heavy stages.";
  }
  if (normalized === "best_quality") {
    return "Best-quality profile favors stronger models where reliability matters more.";
  }
  if (normalized === "manual") {
    return "Manual mode pins one model for all stages; use only when you want fixed behavior.";
  }
  return "Cheap+learned profile keeps costs low while applying learned routing defaults by stage.";
}

function formatRoutingSummary(tiers = []) {
  if (!Array.isArray(tiers) || tiers.length === 0) return "learned routing";
  const bucket = new Map();
  for (const tier of tiers) {
    const model = String(tier?.model || "").trim() || "auto";
    const tierLabel = ROUTING_TIER_LABELS[tier?.tier] || String(tier?.tier || "tier");
    const line = `${tierLabel}:${model}`;
    if (!bucket.has(model)) {
      bucket.set(model, []);
    }
    bucket.get(model).push(line);
  }
  return [...bucket.entries()]
    .map(([model, lines]) =>
      lines.length > 1
        ? `${lines.map((line) => line.split(":")[0]).join(",")}→${model}`
        : lines[0]
    )
    .join(" · ");
}

function formatUsd(value) {
  if (typeof value !== "number" || Number.isNaN(value) || !Number.isFinite(value)) return "—";
  return `$${value < 1e-3 ? value.toFixed(8) : value.toFixed(4)}`;
}

function formatRate(value) {
  if (typeof value !== "number" || Number.isNaN(value) || !Number.isFinite(value)) return "—";
  if (value <= 0) return "free";
  return `$${(value * 1_000_000).toFixed(6)}/M`;
}

function numericPricingValue(value) {
  if (value == null || value === "") return null;
  const parsed = Number(value);
  if (Number.isNaN(parsed) || !Number.isFinite(parsed) || parsed < 0) return null;
  return parsed;
}

function estimateCost(tier, promptTokens, completionTokens) {
  const pricing = tier?.pricing_raw || {};
  const prompt = pricing.prompt ?? pricing.input;
  const completion = pricing.completion ?? pricing.output;
  const promptCost = numericPricingValue(prompt);
  const completionCost = numericPricingValue(completion);

  if ((promptCost == null || promptCost === 0) && (completionCost == null || completionCost === 0)) {
    const request = numericPricingValue(pricing.request);
    if (request != null) return request;
    return null;
  }

  const promptRate = promptCost == null ? 0 : promptCost;
  const completionRate = completionCost == null ? 0 : completionCost;
  return promptRate * promptTokens + completionRate * completionTokens;
}

function catalogPrompt(inputModel) {
  const promptRate = numericPricingValue(
    inputModel?.prompt_rate ?? inputModel?.pricing_raw?.prompt ?? inputModel?.pricing_raw?.input
  );
  const completionRate = numericPricingValue(
    inputModel?.completion_rate ?? inputModel?.pricing_raw?.completion ?? inputModel?.pricing_raw?.output
  );
  if (promptRate == null && completionRate == null) {
    const request = numericPricingValue(inputModel?.request_rate ?? inputModel?.pricing_raw?.request);
    if (request != null) return `request ${formatRate(request)}`;
    return "pricing unknown";
  }
  if (promptRate == null) return `output ${formatRate(completionRate)}`;
  if (completionRate == null) return `input ${formatRate(promptRate)}`;
  return `${formatRate(promptRate)} / ${formatRate(completionRate)}`;
}

function routingPinSummary(tiers, modelOverride, preferredModel) {
  if (!Array.isArray(tiers) || tiers.length === 0) return null;
  const normalizedModels = tiers
    .map((tier) => String(tier?.model || "").trim())
    .filter(Boolean);
  if (!normalizedModels.length) return null;

  const model = normalizedModels[0];
  const allSameModel = normalizedModels.every((item) => item === model);
  if (!allSameModel) return null;

  const sources = new Set(
    tiers.map((tier) => String(tier?.source || "learned").toLowerCase())
  );
  const prettySources = Array.from(sources).map((source) => `${source}`).join("/");

  if (modelOverride) {
    return `all tiers forced to one model from your build override (${model}); clear override to let routing vary by tier`;
  }
  if (preferredModel) {
    return `all tiers forced to one model from preferred-model setting (${model}); clear preferred model in Settings to resume per-tier routing`;
  }
  if (sources.size === 1 && sources.has("manual")) {
    return `all tiers locked manually to one model (${model}); use Settings routing pins per tier for variation`;
  }
  if (sources.size === 1 && sources.has("learned")) {
    return `all tiers resolved to one model (${model}) by current learned/default policy`;
  }
  return `${prettySources} routing resolved this tier-set to one model (${model})`;
}

function describeRoutingLocks(secretsData, modelOverride, routingTiers) {
  const preferred = String(secretsData?.preferred_model || "").trim();
  const modelPins =
    secretsData && typeof secretsData.model_pins === "object" && secretsData.model_pins
      ? secretsData.model_pins
      : {};
  const pinGroups = new Map();
  for (const [tier, value] of Object.entries(modelPins)) {
    const model = String(value || "").trim();
    if (!model) continue;
    const entry = pinGroups.get(model) || [];
    entry.push(tier);
    pinGroups.set(model, entry);
  }
  const pinLines = [...pinGroups.entries()].map(
    ([model, tiers]) => `route pin ${tiers.join(",")}: ${model}`,
  );

  const codegenOpenrouter = String(secretsData?.openrouter_codegen_model || "").trim();
  const codegenCli = String(secretsData?.codegen_cli_model || "").trim();
  const codegenProvider = String(secretsData?.codegen_cli_provider || "").trim();

  const override = String(modelOverride || "").trim();
  const allSameManual =
    Array.isArray(routingTiers) &&
    routingTiers.length > 0 &&
    new Set(routingTiers.map((tier) => tier?.source)).size === 1 &&
    String(routingTiers[0]?.source || "").toLowerCase() === "manual";

  const lines = [];
  if (override) lines.push(`build override: ${override}`);
  if (preferred) lines.push(`preferred model: ${preferred}`);
  if (pinLines.length) lines.push(...pinLines);
  if (codegenOpenrouter) lines.push(`codegen model: ${codegenOpenrouter}`);
  if (codegenCli) lines.push(`codegen CLI model: ${codegenCli}`);
  if (codegenProvider && !codegenCli) lines.push(`codegen provider: ${codegenProvider}`);
  if (allSameManual && !override && !preferred && pinLines.length === 0) {
    const model = String(routingTiers[0]?.model || "").trim();
    if (model) lines.push(`routing manual lock: ${model}`);
  }

  return lines;
}

// Curated one-liner starters spanning the supported stacks. Clicking one fills
// the brief box so a new user has a good, varied jumping-off point.
const EXAMPLE_BRIEFS = [
  "A golf website for adult beginners with lesson paths, drills, equipment basics, tutorial resources, and tee-time CTAs",
  "A local HVAC company website with service pages, financing calls-to-action, reviews, emergency contact, and generated service photos",
  "A client portal web app for a small marketing agency: projects, approvals, invoices, messages, and admin settings",
  "An AI paper-trading dashboard using OpenRouter models, Alpaca paper mode, risk profiles, backtests, and audit logs",
  "A course marketplace for woodworking classes with instructor profiles, schedule filters, checkout mockup, and learner dashboard",
  "A rebuild of a GitHub repo as a polished web UI with the same core features, better onboarding, and editable settings",
  "A mobile habit tracker with streaks, reminders, charts, and offline-first local storage",
  "An MCP server for filesystem search and safe read-only project inspection",
];

// Fallback stack ids if GET /stacks is empty/unavailable — the six real builders.
const FALLBACK_STACKS = [
  { id: "react", description: "a browser web app / SPA / dashboard UI (Vite + React)" },
  { id: "react_native", description: "a mobile app for iOS/Android (Expo)" },
  { id: "fastapi", description: "a Python web app or HTTP/REST API with a server + storage" },
  { id: "static", description: "a static website / landing page (HTML/CSS/JS, no backend)" },
  { id: "python", description: "a Python CLI tool, script, or library (no web UI)" },
  { id: "express", description: "a Node.js web server / API" },
];

// Fan-out is optional; start empty so highlighted chips never look like the
// normal build route. A regular Forge build auto-picks from the brief.
const DEFAULT_STACK_SELECTION = [];

const BUILD_PROFILES = [
  {
    id: "cheap_learned",
    label: "Cheap + learned",
    hint: "Uses learned routing, skills, and recall without pinning an expensive model.",
  },
  {
    id: "fast",
    label: "Fast",
    hint: "One complete candidate; larger apps use concurrent code specialists.",
  },
  {
    id: "balanced",
    label: "Balanced",
    hint: "Keeps cheap routing with a little extra reliability for cleaner results.",
  },
  { id: "best_quality", label: "Best quality", hint: "Runs best-of-N, richer assets when configured, and visual repair." },
  { id: "manual", label: "Manual model", hint: "Pin one OpenRouter model for this build." },
];

const BUILD_ACTIVE_STATUSES = new Set([
  "running",
  "queued",
  "queued_no_studio",
  "pending",
  "awaiting_approval",
]);

const BUILD_TERMINAL_STATUSES = new Set([
  "completed",
  "completed_no_go",
  "cancelled",
  "failed",
  "approved",
  "rejected",
  "interrupted",
]);

function normalizeBuildStatus(status) {
  return String(status || "").trim().toLowerCase();
}

function isActiveBuild(status) {
  return BUILD_ACTIVE_STATUSES.has(normalizeBuildStatus(status));
}

function isTerminalBuild(status) {
  return BUILD_TERMINAL_STATUSES.has(normalizeBuildStatus(status));
}

function studioBuildRefetchInterval(query) {
  const data = query?.state?.data;
  const rows = Array.isArray(data) ? data : data?.builds || [];
  return rows.some((build) => isActiveBuild(build?.status)) ? 5000 : false;
}

// The forge line. Each stage is a node on a horizontal rail: it ignites EMBER
// while running, cools to PLASMA when done, sits ASH while pending, flares the
// hot ember on failure. It also shows WHICH agent ran the stage and its score —
// data the backend already emits — so the line is legible, not just decorative.
function ForgeStage({ s }) {
  const state = s.state;
  const hot = state === "running";
  const done = state === "done";
  const failed = state === "failed";
  const agent = s.agentName || s.agentType;

  const nodeCls = hot
    ? "border-ember/60 bg-ember/10 ring-heat animate-emberflare"
    : done
    ? "border-plasma/40 bg-plasma/5"
    : failed
    ? "border-ember/60 bg-ember/10"
    : "border-hairline bg-void/60";

  const dotCls = hot
    ? "bg-ember animate-forgepulse"
    : done
    ? "bg-plasma"
    : failed
    ? "bg-ember"
    : "bg-ash/40";

  const labelCls = hot
    ? "text-ember"
    : done
    ? "text-plasma"
    : failed
    ? "text-ember"
    : "text-ash";

  const title =
    `${s.stage}${agent ? " · " + agent : ""} · ${failed ? "failed" : state}` +
    (s.score != null ? ` · score ${s.score}` : "");

  return (
    <div
      title={title}
      className={`flex min-w-[7.5rem] flex-col gap-2 rounded-md border px-3 py-2.5 transition-all duration-300 ${nodeCls}`}
    >
      <div className="flex items-center gap-2">
        <span className={`h-1.5 w-1.5 rounded-full ${dotCls}`} />
        <span className={`font-mono text-[11px] ${labelCls}`}>{s.stage}</span>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="eyebrow truncate text-[9px] text-ash/70">
          {failed ? "failed" : agent || state}
        </span>
        {done && s.score != null ? (
          <span className="font-mono text-[9px] text-plasma">{s.score}</span>
        ) : null}
      </div>
    </div>
  );
}

function buildMeta(build) {
  const cls = build.classification || {};
  return {
    stack: build.stack || build.stack_selection?.stack || "auto",
    appType: build.app_type || cls.app_type || "auto",
    engine: build.engine || cls.engine || "auto",
  };
}

function aiMeta(build) {
  const trace = build.model_trace || {};
  const scorecard = build.quality_scorecard || {};
  const stageCount = Array.isArray(trace.stages) ? trace.stages.length : 0;
  const codegenModel = String(trace.codegen_model || "");
  const modelOverride = String(trace.model_override || "");
  const modelSource =
    modelOverride && (!codegenModel || codegenModel === modelOverride)
      ? "manual"
      : codegenModel ? "codegen" : "auto route";
  return {
    profile: build.build_profile || trace.profile || "cheap_learned",
    model: codegenModel || modelOverride || "auto",
    modelSource,
    backend: trace.backend || "auto",
    promptCount: trace.prompt_count || 0,
    stageCount,
    skills: Array.isArray(build.skills_used)
      ? build.skills_used.length
      : scorecard.skills_count || 0,
    recall: Array.isArray(build.recall_used)
      ? build.recall_used.length
      : scorecard.recall_count || 0,
    proof: scorecard.proof_passed,
    build: scorecard.build,
    ...aiCostMeta(build),
  };
}

function aiCostMeta(build) {
  const trace = build.model_trace || {};
  const quality_scorecard = build.quality_scorecard || {};
  const costTruth = build.cost_truth || quality_scorecard.cost_truth || {};
  const externalAssets = costTruth.external_asset_usage || {};
  const runCost = numericPricingValue(
    costTruth.llm_cost_usd ?? build.cost_usd ?? quality_scorecard?.cost_usd
  );
  const stageCosts = Array.isArray(trace.stage_costs)
    ? trace.stage_costs
    : [];
  const stageCostTotal = stageCosts.reduce((sum, item) => {
    const value = numericPricingValue(item?.cost_usd ?? item?.cost);
    return value == null ? sum : sum + value;
  }, 0);
  const stageCostCount = stageCosts.filter((item) => {
    const value = numericPricingValue(item?.cost_usd ?? item?.cost);
    return value != null;
  }).length;
  const costQualifier = String(
    costTruth.llm_cost_label || "estimated LLM (source unavailable)"
  );
  const externalPredictionCount = Number(externalAssets.prediction_count || 0);
  const externalAttemptCount = Number(
    externalAssets.attempt_count || externalPredictionCount
  );
  const historicalAssetCount = Number(
    externalAssets.historical_generated_asset_count || 0
  );
  const externalAssetLabel = externalAttemptCount > 0
    ? `+ ${externalPredictionCount} confirmed Replicate prediction${externalPredictionCount === 1 ? "" : "s"}${externalAttemptCount > externalPredictionCount ? ` / ${externalAttemptCount} attempts` : ""}; dollars unavailable`
    : historicalAssetCount > 0
      ? `+ ${historicalAssetCount} historical Replicate asset${historicalAssetCount === 1 ? "" : "s"}; provider dollars unavailable`
    : "no external asset charge recorded";
  const unconfirmedExposure = numericPricingValue(
    costTruth.max_unconfirmed_exposure_usd
  );

  return {
    costLabel: runCost == null ? "—" : formatUsd(runCost),
    costQualifier,
    externalAssetLabel,
    costTitle: [
      `${runCost == null ? "unknown" : formatUsd(runCost)} ${costQualifier}`,
      externalAssetLabel,
      unconfirmedExposure > 0
        ? `up to ${formatUsd(unconfirmedExposure)} unconfirmed timeout exposure`
        : "",
    ].filter(Boolean).join(" · "),
    stageCostLabel: stageCostCount
      ? `${formatUsd(stageCostTotal)} / ${stageCostCount}`
      : "—",
  };
}

function buildDiagnostics(build) {
  const trace = build.model_trace || {};
  const agentic = trace.agentic || {};
  const scorecard = build.quality_scorecard || {};
  const skills = Array.isArray(build.skills_used)
    ? build.skills_used.length
    : scorecard.skills_count || 0;
  const recall = Array.isArray(build.recall_used)
    ? build.recall_used.length
    : scorecard.recall_count || 0;
  const issues = [];
  if (scorecard.proof_passed === false) issues.push("proof failed");
  if (scorecard.build && scorecard.build !== "passed") {
    issues.push(`build ${scorecard.build}`);
  }
  const financeSanity = scorecard.finance_sanity || {};
  if (financeSanity.ok === false) {
    const financeIssue = Array.isArray(financeSanity.issues)
      ? String(financeSanity.issues[0] || "").trim()
      : "";
    issues.push(
      financeIssue ? `finance sanity: ${financeIssue}` : "finance sanity failed"
    );
  }
  const workflowDepth = scorecard.workflow_depth || {};
  if (workflowDepth.ok === false) {
    const missingWorkflow = Array.isArray(workflowDepth.missing)
      ? workflowDepth.missing
          .map((item) => String(item || "").trim())
          .filter(Boolean)
          .slice(0, 2)
      : [];
    const workflowIssue = Array.isArray(workflowDepth.issues)
      ? String(workflowDepth.issues[0] || "").trim()
      : "";
    const workflowMessage = missingWorkflow.length
      ? `workflow depth: missing ${missingWorkflow.join(", ")}`
      : workflowIssue
        ? `workflow depth: ${workflowIssue}`
        : "workflow depth failed";
    issues.push(workflowMessage);
  }
  if (agentic.stalled) {
    const stallReason = String(agentic.stall_reason || agentic.error || "").trim();
    issues.push(stallReason ? `agentic stalled: ${stallReason}` : "agentic stalled");
  }
  if (agentic.fallback_model) {
    issues.push(`agentic fallback: ${agentic.fallback_model}`);
  }
  if (build.status === "failed") issues.push("failed");
  if (skills === 0) issues.push("skills 0");
  if (recall === 0) issues.push("recall 0");
  if ((trace.prompt_count || 0) === 0 && ["completed", "completed_no_go"].includes(build.status)) {
    issues.push("prompts 0");
  }
  return issues.length ? issues.join(" · ") : "no obvious gaps";
}

function rebuildFields(build) {
  const manifest = build.manifest && typeof build.manifest === "object" ? build.manifest : {};
  const extra = manifest.extra && typeof manifest.extra === "object" ? manifest.extra : {};
  const trace = build.model_trace || {};
  const profile =
    build.build_profile ||
    trace.profile ||
    extra.build_profile ||
    "cheap_learned";
  return {
    brief: String(manifest.brief || build.brief || ""),
    stack: "",
    slug: String(manifest.slug || build.slug || ""),
    buildProfile: BUILD_PROFILES.some((p) => p.id === profile)
      ? profile
      : "cheap_learned",
    sourceBuildProfile: profile === "full_app" ? "full_app" : null,
    modelOverride: String(trace.model_override || extra.model_override || ""),
    fullApp: Boolean(
      trace.full_app ||
        extra.full_app_contract ||
        profile === "full_app"
    ),
  };
}

export default function Studio({ stream }) {
  const qc = useQueryClient();
  const [brief, setBrief] = useState("");
  // Fan-out stack selection as a set of stack ids (toggleable chips below).
  const [selectedStacks, setSelectedStacks] = useState(
    () => new Set(DEFAULT_STACK_SELECTION)
  );
  const [pendingBuildId, setPendingBuildId] = React.useState(null);
  const briefRef = useRef(null);
  // "Build from a picture": an optional reference image (data URL) attached to
  // the build. No attachment -> the field is omitted from the POST (unchanged).
  const [refImage, setRefImage] = useState(null); // { url, name }
  const fileInputRef = useRef(null);
  const [buildProfile, setBuildProfile] = useState("cheap_learned");
  const [fullApp, setFullApp] = useState(false);
  const [modelOverride, setModelOverride] = useState("");
  const [variantSource, setVariantSource] = useState(null);
  const [showRoutingDetails, setShowRoutingDetails] = useState(false);
  const [modelCatalogOpen, setModelCatalogOpen] = useState(false);
  const [modelCatalogQuery, setModelCatalogQuery] = useState("");
  const [modelCatalogSort, setModelCatalogSort] = useState("created");
  const [modelCatalogOrder, setModelCatalogOrder] = useState("desc");
  const [modelCatalogPage, setModelCatalogPage] = useState(0);
  const [catalogRefreshing, setCatalogRefreshing] = useState(false);

  const onPickImage = (file) => {
    if (!file || !file.type?.startsWith("image/")) return;
    const reader = new FileReader();
    reader.onload = () => setRefImage({ url: String(reader.result), name: file.name });
    reader.readAsDataURL(file);
  };
  const clearImage = () => {
    setRefImage(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const { data: builds } = useQuery({
    queryKey: ["builds"],
    queryFn: queryFn("/builds"),
    refetchInterval: studioBuildRefetchInterval,
  });

  // Supported real-builder stacks for the fan-out picker. Falls back to the six
  // known ids if the endpoint is empty/unavailable.
  const { data: stacksData } = useQuery({
    queryKey: ["stacks"],
    queryFn: queryFn("/stacks"),
  });
  const stackOptions = useMemo(() => {
    const opts = stacksData?.stacks;
    return Array.isArray(opts) && opts.length > 0 ? opts : FALLBACK_STACKS;
  }, [stacksData]);

  const models = useQuery({
    queryKey: ["models"],
    queryFn: queryFn("/models"),
    retry: 0,
  });
  const catalog = useQuery({
    queryKey: [
      "models",
      "catalog",
      modelCatalogQuery,
      modelCatalogSort,
      modelCatalogOrder,
      modelCatalogPage,
    ],
    queryFn: () => {
      const params = new URLSearchParams({
        limit: String((modelCatalogPage + 1) * MODEL_CATALOG_PAGE_SIZE),
        sort: modelCatalogSort,
        order: modelCatalogOrder,
        refresh: catalogRefreshing,
      });
      const q = modelCatalogQuery.trim();
      if (q) {
        params.set("q", q);
      }
      return apiFetch(`/models/catalog?${params.toString()}`);
    },
    retry: 0,
  });
  useEffect(() => {
    setModelCatalogPage(0);
  }, [modelCatalogQuery, modelCatalogSort, modelCatalogOrder]);
  const secrets = useQuery({
    queryKey: ["llm-secrets"],
    queryFn: queryFn("/llm/secrets"),
    retry: 0,
  });
  const normalizeModelId = (value) => value.replace(/\s+/g, "").trim();
  const routingSecrets = secrets.data || {};
  const routingFreeOnly = routingSecrets.free_only !== false;
  const routingModelPins =
    routingSecrets && typeof routingSecrets.model_pins === "object" && routingSecrets.model_pins
      ? routingSecrets.model_pins
      : {};
  const effectiveBuildProfile =
    fullApp && variantSource?.sourceBuildProfile
      ? variantSource.sourceBuildProfile
      : buildProfile;
  const normalizedModelOverride = normalizeModelId(modelOverride);
  const modelOptions = models.data?.models || [];
  const routingPreview = useQuery({
    queryKey: [
      "routing-preview",
      effectiveBuildProfile,
      normalizedModelOverride,
      String(routingSecrets.preferred_model || ""),
      String(routingSecrets.codegen_cli_model || ""),
      String(routingSecrets.openrouter_codegen_model || ""),
      String(routingSecrets.free_only ?? ""),
      String(routingModelPins.cheap || ""),
      String(routingModelPins.ui || ""),
      String(routingModelPins.backend || ""),
      String(routingModelPins.strong || ""),
      String(routingModelPins.docs || ""),
    ],
    queryFn: () => {
      const params = new URLSearchParams();
      params.set("build_profile", effectiveBuildProfile);
      if (normalizedModelOverride) {
        params.set("model_override", normalizedModelOverride);
      }
      return apiFetch(`/models/routing-preview?${params.toString()}`);
    },
    retry: 0,
  });
  const modelCatalogRows = Array.isArray(catalog.data?.items)
    ? catalog.data.items
    : [];
  const manualModelChoices = modelCatalogRows;
  const catalogTotal = catalog.data?.count ?? 0;
  const catalogShown = catalog.data?.returned ?? modelCatalogRows.length;
  const catalogShowingAll = catalogShown >= catalogTotal || catalogTotal === 0;
  const unknownModelOverride =
    !!normalizedModelOverride &&
    modelOptions.length > 0 &&
    !modelOptions.includes(normalizedModelOverride);
  const routingTiers = Array.isArray(routingPreview.data?.tiers)
    ? routingPreview.data.tiers
    : [];
  const selectedManualModelValue =
    findModelValue(modelCatalogRows, normalizedModelOverride) ||
    routingTiers.find((tier) => tier?.model === normalizedModelOverride) ||
    null;
  const exampleWorkload =
    catalog.data?.example_workload || routingPreview.data?.example_workload;
  const routingCatalogInfo = (() => {
    const count = routingPreview.data?.catalog_model_count;
    const age = routingPreview.data?.catalog_age_seconds;
    const ageLabel =
      typeof age === "number" ? `${Math.max(1, Math.round(age))}s ago` : null;
    return [
      count != null ? `${count} models in OpenRouter catalog` : null,
      ageLabel ? `updated ${ageLabel}` : null,
      routingPreview.data?.catalog_note ? `catalog: ${routingPreview.data.catalog_note}` : null,
    ].filter(Boolean).join(" · ");
  })();
  const routingLockLines = describeRoutingLocks(
    secrets.data,
    normalizedModelOverride,
    routingTiers,
  );
  const hasRoutingLocks = routingLockLines.length > 0;
  const routingSummaryWithCost = routingTiers
    .slice(0, 4)
    .map((tier) => {
      const tierLabel = ROUTING_TIER_LABELS[tier.tier] || tier.tier;
      const tierCost = tier?.cost_estimates_usd?.prompt_1k_completion_1k;
      return `${tierLabel}:${String(tier?.model || "auto")}${
        typeof tierCost === "number"
          ? ` · ${formatUsd(tierCost)} · 1k×1k`
        : ""
      }`;
    })
    .join(" · ");
  const routingPin = routingPinSummary(
    routingTiers,
    normalizedModelOverride,
    routingPreview.data?.preferred_model
  );

  const toggleStack = (id) =>
    setSelectedStacks((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  const useExample = (text) => {
    setBrief(text);
    // Focus + scroll the brief box so the user can edit then build.
    const el = briefRef.current;
    if (el) {
      el.focus();
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };

  const chooseBuildProfile = (id) => {
    setBuildProfile(id);
    setVariantSource((prev) =>
      prev?.sourceBuildProfile ? { ...prev, sourceBuildProfile: null } : prev
    );
  };
  const toggleFullApp = (checked) => {
    setFullApp(checked);
    if (!checked) {
      setVariantSource((prev) =>
        prev?.sourceBuildProfile ? { ...prev, sourceBuildProfile: null } : prev
      );
    }
  };

  const loadRebuildVariant = (build) => {
    const fields = rebuildFields(build);
    if (!fields.brief.trim()) return;
    setBrief(fields.brief);
    setBuildProfile(fields.buildProfile);
    setFullApp(fields.fullApp);
    setModelOverride(fields.modelOverride);
    setVariantSource({
      build_id: String(build.build_id || ""),
      slug: fields.slug,
      stack: fields.stack,
      sourceBuildProfile: fields.sourceBuildProfile,
    });
    clearImage();
    const el = briefRef.current;
    if (el) {
      el.focus();
      el.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  };

  const submit = useMutation({
    mutationFn: (payload) => apiPost("/builds", payload),
    onSuccess: () => {
      setBrief("");
      clearImage();
      setVariantSource(null);
      qc.invalidateQueries({ queryKey: ["builds"] });
    },
  });

  const approve = useMutation({
    mutationFn: ({ build_id, approved }) =>
      apiPost("/studio/approve", { build_id, approved, reason: "" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["builds"] }),
    onSettled: () => setPendingBuildId(null),
  });
  const cancelBuild = useMutation({
    mutationFn: ({ build_id }) =>
      apiPost("/builds/cancel", {
        build_id,
        reason: "cancelled from Studio",
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["builds"] }),
    onSettled: () => setPendingBuildId(null),
  });

  const cleanupBuild = useMutation({
    mutationFn: ({ build_id }) => apiPost("/builds/cleanup", { build_id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["builds"] }),
    onSettled: () => setPendingBuildId(null),
  });

  const cleanupCompletedBuilds = useMutation({
    mutationFn: () => apiPost("/builds/cleanup", { all_terminal: true }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["builds"] }),
  });

  const setFreeOnlyRouting = useMutation({
    mutationFn: (free_only) => apiPost("/llm/routing", { free_only }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["llm-secrets"] });
      qc.invalidateQueries({ queryKey: ["routing-preview"] });
    },
  });

  const clearRoutingLocks = useMutation({
    mutationFn: async () => {
      await apiPost("/settings/model", { model: "" });
      await apiPost("/llm/routing", {
        codegen_cli_provider: "",
        codegen_cli_model: "",
        openrouter_codegen_model: "",
        model_pins: {
          cheap: "",
          ui: "",
          backend: "",
          strong: "",
          docs: "",
        },
      });
    },
    onSuccess: () => {
      setModelOverride("");
      qc.invalidateQueries({ queryKey: ["llm-secrets"] });
      qc.invalidateQueries({ queryKey: ["routing-preview"] });
      qc.invalidateQueries({ queryKey: ["models"] });
    },
  });

  // Spec 4: explore the brief across divergent stacks; results stream as
  // FANOUT_* events on the shared socket. The selected stack ids are sent as a
  // comma-joined string (the /studio/fanout contract).
  const fanoutMut = useMutation({
    mutationFn: () => {
      const payload = {
        brief: brief.trim(),
        stacks: [...selectedStacks].join(","),
        build_profile: effectiveBuildProfile,
        full_app: fullApp,
      };
      if (normalizedModelOverride) {
        payload.model_override = normalizedModelOverride;
      }
      if (refImage?.url) {
        payload.reference_image = refImage.url;
      }
      return apiPost("/studio/fanout", payload);
    },
  });

  const events = stream?.events || [];
  // Driven off the REAL emitted plan (build.started.stages) + the per-stage
  // agent/score/cost/gaps the backend already streams — not a hardcoded list.
  const pipeline = useMemo(() => pipelineFromEvents(events, STAGES), [events]);

  const fanout = useMemo(() => {
    const cands = {};
    let done = null;
    let active = false;
    for (const e of events) {
      if (e.type === "fanout.started") active = true;
      else if (e.type === "fanout.candidate") {
        const p = e.payload || {};
        if (p.candidate_id) cands[p.candidate_id] = p;
      } else if (e.type === "fanout.completed") {
        done = e.payload || {};
        active = false;
      }
    }
    return { cands: Object.values(cands), done, active };
  }, [events]);

  const recentBuilds = Array.isArray(builds) ? builds : builds?.builds || [];
  const terminalCleanupCandidates = recentBuilds.filter((b) => isTerminalBuild(b.status));
  const assetState = secrets.data?.replicate
    ? secrets.data?.asset_gen
      ? {
          tone: "plasma",
          label: "assets ready",
          title: `Replicate ${secrets.data?.replicate_model || "default"} is configured for generated assets`,
        }
      : {
          tone: "ember",
          label: "asset gen off",
          title: "Replicate key is configured, but asset generation is off",
        }
    : {
        tone: "ash",
        label: "no Replicate",
        title: "No Replicate key configured for generated assets",
      };
  const effectiveProfileLabel =
    effectiveBuildProfile === "full_app"
      ? "Full app contract"
      : BUILD_PROFILES.find((p) => p.id === effectiveBuildProfile)?.label || effectiveBuildProfile;
  const buildIntent = {
    mode: `${effectiveProfileLabel}${
      fullApp ? " · full app" : ""
    }`,
    model: (() => {
      const tiers = routingPreview.data?.tiers;
      if (Array.isArray(tiers) && tiers.length > 0) {
        return formatRoutingSummary(tiers);
      }
      return normalizedModelOverride || "learned routing";
    })(),
    reference: refImage?.name || "no reference image",
    fanout: selectedStacks.size
      ? `${selectedStacks.size} stack${selectedStacks.size === 1 ? "" : "s"} · ${[
          ...selectedStacks,
        ].join(", ")}${selectedStacks.size === 1 ? " · add one more" : ""}`
      : "auto stack",
  };
  const routingPolicy = routingPolicyHint(routingPreview.data?.build_profile || effectiveBuildProfile);
  const routingRows = routingTiers.map((tier) => {
    const costRows = ROUTING_PREVIEW_ESTIMATES.map(({ key, label, promptTokens, completionTokens }) => {
      const estimated = tier.cost_estimates_usd?.[key] ??
        estimateCost(tier, promptTokens, completionTokens);
      if (estimated == null) return null;
      return (
        <span key={key} className="inline-flex items-center justify-between gap-1 rounded border border-hairline/50 px-1.5 py-0.5">
          <span className="font-mono text-[9px] text-ash/80">{label}</span>
          <span className="font-mono text-[9px] text-ash">{formatUsd(estimated)}</span>
        </span>
      );
    }).filter(Boolean);

    const pricing = tier.pricing_raw || {};
    const promptRate = formatRate(
      numericPricingValue(pricing.prompt ?? pricing.input)
    );
    const completionRate = formatRate(
      numericPricingValue(pricing.completion ?? pricing.output)
    );
    const modelValue = describeModelValue(
      tier,
      ROUTING_TIER_LABELS[tier.tier] || tier.tier,
    );

    return {
      key: tier.tier,
      tier: tier.tier,
      model: tier.model,
      source: tier.source,
      row: (
        <div key={tier.tier} className="min-w-0 space-y-1.5 text-[10px]">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <Pill tone={tier.source === "manual" ? "ember" : "plasma"}>{tier.source}</Pill>
            <span className="text-bone capitalize">{tier.tier}</span>
            <span className="min-w-0 break-words text-ash">{tier.model}</span>
          </div>
          <div className="flex flex-wrap gap-x-3 text-[9px] text-ash">
            <span>
              input <span className="text-plasma">{promptRate}</span>
            </span>
            <span>·</span>
            <span>
              output <span className="text-plasma">{completionRate}</span>
            </span>
            <span>·</span>
            <span className="text-plasma">{tier.pricing}</span>
          </div>
          {costRows.length ? (
            <div className="grid min-w-0 grid-cols-1 gap-1 sm:grid-cols-3">
              {costRows}
            </div>
          ) : null}
          {modelValue ? (
            <div className="break-words font-mono text-[9px] text-ash/80">
              {modelValue}
            </div>
          ) : null}
        </div>
      ),
      costs: null,
    };
  });
  const routingSummary = routingTiers
    .slice(0, 4)
    .map((tier) => `${ROUTING_TIER_LABELS[tier.tier] || tier.tier}: ${tier.model}`)
    .join(" · ");

  const running = pipeline.filter((p) => p.state === "running").length;
  const done = pipeline.filter((p) => p.state === "done").length;

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Build Console"
        title="Studio"
        sub="Forge a brief into running software. Watch the line ignite as the swarm works."
        actions={
          <span className="badge border-hairline text-ash">
            forge line ·{" "}
            <span className={`ml-1 ${running ? "text-ember" : "text-plasma"}`}>
              {running ? "live" : "idle"}
            </span>
          </span>
        }
      />

      <div className="mb-6 grid gap-4 xl:grid-cols-[minmax(0,1fr)_20rem] xl:items-start">
        <div className="min-w-0 space-y-4">
          <Panel className="p-4">
            <form
              className="flex flex-col gap-3 sm:flex-row sm:items-stretch"
              onSubmit={(e) => {
                e.preventDefault();
                if (!brief.trim()) return;
                const payload = {
                  brief: brief.trim(),
                  build_profile: effectiveBuildProfile,
                  full_app: fullApp,
                };
                if (variantSource?.stack) {
                  payload.stack = variantSource.stack;
                }
                if (normalizedModelOverride) {
                  payload.model_override = normalizedModelOverride;
                }
                if (refImage?.url) payload.reference_image = refImage.url;
                submit.mutate(payload);
              }}
            >
              <input
                ref={briefRef}
                className="field flex-1"
                placeholder="Describe the app to build…"
                aria-label="App brief"
                value={brief}
                onChange={(e) => setBrief(e.target.value)}
              />
              {/* "Build from a picture": attach one reference image (screenshot,
                  drawing, or diagram) that the design/architecture agents match. */}
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                aria-label="Reference image"
                className="hidden"
                onChange={(e) => onPickImage(e.target.files?.[0])}
              />
              {refImage ? (
                <div className="flex items-center gap-2 rounded border border-hairline px-2 py-1">
                  <img
                    src={refImage.url}
                    alt={"Reference preview: " + refImage.name}
                    className="h-9 w-9 rounded object-cover"
                  />
                  <span className="max-w-[8rem] truncate font-mono text-[11px] text-ash">
                    {refImage.name}
                  </span>
                  <button
                    type="button"
                    onClick={clearImage}
                    aria-label="Remove reference image"
                    title="Remove reference image"
                    className="text-ash hover:text-ember"
                  >
                    ✕
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                  className="btn-ghost disabled:opacity-50"
                  title="Attach a reference image — a screenshot, drawing, or diagram to match"
                >
                  + Image
                </button>
              )}
              <button
                type="submit"
                disabled={
                  submit.isPending ||
                  !brief.trim()
                }
                className="btn-ember disabled:opacity-50"
              >
                {submit.isPending ? "Forging…" : "Forge build"}
              </button>
            </form>
            {submit.isError ? (
              <ErrorText className="mt-3 max-h-28">
                {String(submit.error.message)}
              </ErrorText>
            ) : null}

            <div className="mt-3 border-t border-hairline pt-3">
              <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
                <div className="flex flex-wrap gap-2 md:col-span-2">
                  {BUILD_PROFILES.map((p) => {
                    const on = buildProfile === p.id;
                    return (
                      <button
                        key={p.id}
                        type="button"
                        onClick={() => chooseBuildProfile(p.id)}
                        title={p.hint}
                        aria-pressed={on}
                        className={`rounded-full border px-3 py-1 font-mono text-[11px] transition-colors ${
                          on
                            ? "border-plasma/50 bg-plasma/10 text-plasma"
                            : "border-hairline text-ash hover:border-plasma/30 hover:text-bone"
                        }`}
                      >
                        {p.label}
                      </button>
                    );
                  })}
                </div>
                <div className="flex min-w-0 flex-col gap-1">
                  <input
                    type="text"
                    list="studio-models"
                    aria-label="Manual model override"
                    value={modelOverride}
                    onChange={(e) => setModelOverride(e.target.value)}
                    placeholder="openrouter model (optional)"
                    className="field"
                    title="Pin one OpenRouter model for this build"
                  />
                  <datalist id="studio-models">
                    {manualModelChoices.map((item) => (
                      <option
                        key={item.id}
                        value={item.id}
                        label={formatModelOption(item)}
                      />
                    ))}
                  </datalist>
                  <div className="mt-1 flex items-center justify-between gap-2">
                    <span className={`font-mono text-[10px] ${unknownModelOverride ? "text-ember" : "text-ash/60"}`}>
                      {normalizedModelOverride
                        ? unknownModelOverride
                          ? `Model override: ${normalizedModelOverride} (not in cached OpenRouter list)`
                          : `Model override: ${normalizedModelOverride} (for this build)`
                        : models.data?.note || `${(models.data?.models || []).length} OpenRouter models`}
                    </span>
                    {normalizedModelOverride ? (
                      <button
                        type="button"
                        onClick={() => setModelOverride("")}
                        className="btn-ghost py-0.5 text-[10px]"
                      >
                        clear
                      </button>
                    ) : null}
                  </div>
                  {selectedManualModelValue ? (
                    <div className="break-words font-mono text-[10px] text-ash/80">
                      {describeModelValue(selectedManualModelValue, "selected model")}
                    </div>
                  ) : null}
                  <details
                    open={modelCatalogOpen}
                    onToggle={(event) => setModelCatalogOpen(event.currentTarget.open)}
                    className="mt-2 rounded border border-hairline/60"
                  >
                    <summary className="cursor-pointer px-2 py-1 font-mono text-[10px] text-ash">
                      Model catalog explorer
                    </summary>
                    {modelCatalogOpen ? (
                      <div className="border-t border-hairline p-2">
                      <div className="flex flex-wrap items-center gap-2">
                        <input
                          type="text"
                          aria-label="Filter model catalog"
                          value={modelCatalogQuery}
                          onChange={(e) => setModelCatalogQuery(e.target.value)}
                          placeholder="filter model id (e.g. qwen, deepseek, openai/)"
                          className="field min-w-[12rem] flex-1"
                        />
                        <select
                          aria-label="Model catalog sort field"
                          value={modelCatalogSort}
                          onChange={(e) => setModelCatalogSort(e.target.value)}
                          className="field max-w-[8.5rem]"
                        >
                          <option value="created">sort newest</option>
                          <option value="id">sort id</option>
                          <option value="provider">sort provider</option>
                          <option value="price">sort price</option>
                        </select>
                        <select
                          aria-label="Model catalog sort order"
                          value={modelCatalogOrder}
                          onChange={(e) => setModelCatalogOrder(e.target.value)}
                          className="field max-w-[7.5rem]"
                        >
                          <option value="asc">asc</option>
                          <option value="desc">desc</option>
                        </select>
                        <button
                      type="button"
                          className="btn-ghost py-0.5 text-[10px]"
                          onClick={() => {
                            setModelCatalogPage(0);
                            setCatalogRefreshing(true);
                            catalog.refetch().finally(() => {
                              setCatalogRefreshing(false);
                            });
                          }}
                          disabled={catalog.isLoading}
                          title="Refresh model catalog with latest OpenRouter data"
                        >
                          {catalog.isLoading ? "refreshing…" : "refresh"}
                        </button>
                      </div>
                      <div className="mt-1 text-[10px] text-ash/70">
                        {catalog.isLoading
                          ? "loading catalog…"
                          : catalog.data?.note
                            ? catalog.data.note
                            : `${catalogShown} shown · ${catalogTotal} total`}
                      </div>
                      <div className="mt-1 text-[9px] text-ash/70">
                        {describeExampleWorkload(exampleWorkload)}
                      </div>
                      <div className="mt-1 max-h-56 overflow-auto rounded border border-hairline/60">
                        {modelCatalogRows.length ? (
                          modelCatalogRows.map((entry) => (
                            <button
                              key={entry.id}
                              type="button"
                              onClick={() => setModelOverride(entry.id)}
                              className="block w-full border-b border-hairline/60 px-2 py-1.5 text-left transition-colors hover:bg-void/55 last:border-b-0"
                              title={`pin model: ${entry.id}; ${formatModelOption(entry)}`}
                            >
                              <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                                <span className="text-[9px] text-ash/80">{entry.provider}</span>
                                <span className="min-w-0 flex-1 break-all font-mono text-[10px] text-bone">
                                  {entry.id}
                                </span>
                                <span className="text-[9px] text-ash">
                                  {entry.is_free ? "free" : catalogPrompt(entry)}
                                </span>
                                <span className="text-[9px] text-plasma">
                                  example {formatExampleUsd(entry.example_cost_usd)}
                                </span>
                              </span>
                              {entry.value_alternative?.id ? (
                                <span className="mt-0.5 block break-words font-mono text-[9px] text-ash/75">
                                  cheaper peer {entry.value_alternative.id} {formatExampleUsd(entry.value_alternative.example_cost_usd)} · {entry.value_alternative.savings_percent}% less · {entry.value_alternative.reason}
                                </span>
                              ) : null}
                            </button>
                          ))
                        ) : (
                          <div className="px-2 py-2 font-mono text-[10px] text-ash/70">
                            No catalog rows matched.
                          </div>
                        )}
                        {!catalogShowingAll ? (
                          <button
                            type="button"
                            onClick={() => setModelCatalogPage((page) => page + 1)}
                            disabled={catalog.isLoading}
                            className="w-full border-t border-hairline/60 bg-void/35 py-2 text-[10px] font-mono text-plasma hover:bg-void/55 disabled:opacity-50"
                          >
                            {catalog.isLoading ? "loading…" : "load more"}
                          </button>
                        ) : null}
                      </div>
                      </div>
                    ) : null}
                  </details>
                </div>
                <div className="flex flex-wrap items-center gap-2 md:justify-end">
                  <label
                    className="flex items-center gap-2 rounded-md border border-hairline px-3 py-2 font-mono text-[11px] text-ash"
                    title="Force automatic routing to OpenRouter free models"
                  >
                    <input
                      type="checkbox"
                      checked={routingFreeOnly}
                      disabled={setFreeOnlyRouting.isPending || secrets.isLoading}
                      onChange={(e) => setFreeOnlyRouting.mutate(e.target.checked)}
                      className="accent-plasma"
                    />
                    <span className={routingFreeOnly ? "text-plasma" : ""}>Free only</span>
                  </label>
                  <label
                    className="flex items-center gap-2 rounded-md border border-hairline px-3 py-2 font-mono text-[11px] text-ash"
                    title="Build a fuller product with richer content, assets when configured, and more repair budget"
                  >
                    <input
                      type="checkbox"
                      checked={fullApp}
                      onChange={(e) => toggleFullApp(e.target.checked)}
                      className="accent-plasma"
                    />
                    <span className={fullApp ? "text-plasma" : ""}>Full app</span>
                  </label>
                </div>
              </div>
              {variantSource ? (
                <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-hairline pt-3 font-mono text-[11px] text-ash">
                  <Pill tone="ash">
                    variant · {variantSource.stack || "auto stack"}
                  </Pill>
                  <span>
                    Loaded from {variantSource.slug || variantSource.build_id || "previous build"}
                  </span>
                  <button
                    type="button"
                    onClick={() => setVariantSource(null)}
                    className="btn-ghost py-0.5 text-[10px]"
                    title="Return to a new auto-stack build"
                  >
                    clear variant
                  </button>
                </div>
              ) : null}
            </div>
          </Panel>
          <Panel className="p-4">
          <div className="order-3 min-w-0 xl:col-start-1 xl:row-start-2">
            {/* Example briefs: clickable starters that fill the brief box. */}
            <div>
              <span className="eyebrow text-[9px] text-ash/70">Need a starting point? Try:</span>
              <div className="mt-2 flex flex-wrap gap-2">
                {EXAMPLE_BRIEFS.map((ex) => (
                  <button
                    key={ex}
                    type="button"
                    onClick={() => useExample(ex)}
                    title="Fill the brief with this example — then edit and forge"
                    className="rounded-full border border-hairline px-3 py-1 text-left text-[11px] text-ash transition-colors hover:border-ember/40 hover:text-bone"
                  >
                    {ex}
                  </button>
                ))}
              </div>
            </div>

            {/* Spec 4: fan the same brief out across divergent stacks, pick a winner */}
            <div className="mt-3 border-t border-hairline pt-3">
              <div className="flex flex-col gap-1">
                <span className="font-mono text-[11px] text-ash">Fan out across stacks:</span>
                <span className="text-[11px] text-ash/70">
                  Optional — skyn3t auto-picks a stack from your brief. Use fan-out to
                  build across several and compare.
                </span>
              </div>
              <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                <div className="flex flex-wrap gap-2">
                  {stackOptions.map((s) => {
                    const on = selectedStacks.has(s.id);
                    return (
                      <button
                        key={s.id}
                        type="button"
                        onClick={() => toggleStack(s.id)}
                        title={s.description}
                        aria-pressed={on}
                        className={`rounded-full border px-3 py-1 font-mono text-[11px] transition-colors ${
                          on
                            ? "border-ember/50 bg-ember/10 text-ember"
                            : "border-hairline text-ash hover:border-ember/30 hover:text-bone"
                        }`}
                      >
                        {s.id}
                      </button>
                    );
                  })}
                </div>
                <div className="flex flex-shrink-0 gap-2">
                  <button
                    type="button"
                    onClick={() => setSelectedStacks(new Set(["react", "nextjs", "static"]))}
                    className="btn-ghost disabled:opacity-50"
                    title="Select common website/UI stacks for fan-out"
                  >
                    Web set
                  </button>
                  <button
                    type="button"
                    onClick={() => setSelectedStacks(new Set())}
                    disabled={selectedStacks.size === 0}
                    className="btn-ghost disabled:opacity-50"
                    title="Clear fan-out stack selection"
                  >
                    Clear
                  </button>
                </div>
                <button
                  type="button"
                  onClick={() => fanoutMut.mutate()}
                  disabled={
                    fanoutMut.isPending || !brief.trim() || selectedStacks.size < 2
                  }
                  className="btn-ghost flex-shrink-0 disabled:opacity-50"
                  title="Build N divergent stack candidates for this brief and pick the winner"
                >
                  {fanoutMut.isPending ? "Exploring…" : "Fan out"}
                </button>
              </div>
            </div>
            {fanoutMut.isError ? (
              <ErrorText className="mt-2 max-h-24 text-[11px]">
                {String(fanoutMut.error.message)}
              </ErrorText>
            ) : null}
            {fanoutMut.data && fanoutMut.data.accepted === false ? (
              <ErrorText className="mt-2 max-h-24 text-[11px]">
                {fanoutMut.data.reason || "fan-out unavailable"}
              </ErrorText>
            ) : null}
          </div>
          </Panel>
        </div>
          <Panel className="order-2 p-3 xl:order-2">
            <div className="mb-3 border-b border-hairline pb-3">
              <div className="flex items-start justify-between gap-2">
                <div className="eyebrow text-[9px]">Routing estimate</div>
                {routingRows.length ? (
                  <button
                    type="button"
                    onClick={() => setShowRoutingDetails((v) => !v)}
                    className="font-mono text-[9px] text-ash underline decoration-dotted underline-offset-4"
                  >
                    {showRoutingDetails ? "hide details" : "show details"}
                  </button>
                ) : null}
              </div>
              <div className="mt-1 font-mono text-[11px] text-ash">
                backend: {routingPreview.data?.backend || "auto"} · profile:
                {" "}
                {routingPreview.data?.build_profile || effectiveBuildProfile}
                {" "}· {routingFreeOnly ? "free only" : "paid allowed"}
                {routingPreview.isError || routingPreview.isLoading ? " · updating" : ""}
              </div>
              {routingCatalogInfo ? (
                <div className="mt-1 break-words text-[9px] text-ash/70">
                  {routingCatalogInfo}
                </div>
              ) : null}
              {routingPreview.data?.estimate_reason ? (
                <div className="mt-1 break-words text-[9px] text-ash/70">
                  {routingPreview.data.estimate_reason}
                </div>
              ) : null}
              {routingPin ? (
                <div className="mt-1 break-words text-[9px] text-ash/80">
                  {routingPin}
                </div>
              ) : null}
              {routingLockLines.length ? (
                <div className="mt-2">
                  <div className="font-mono text-[9px] text-ash/80">routing locks active:</div>
                  <div className="mt-1 flex flex-wrap gap-1">
                    {routingLockLines.map((line) => (
                      <span
                        key={line}
                        className="rounded-full border border-hairline/70 px-2 py-0.5 text-[9px] text-ash"
                      >
                        {line}
                      </span>
                    ))}
                  </div>
                </div>
              ) : null}
              {routingSummaryWithCost ? (
                <div className="mt-1 break-words text-[10px] text-ash">
                  {routingSummaryWithCost}
                </div>
              ) : null}
              {routingSummary ? (
                <div className="mt-1 break-words text-[9px] text-ash/70">
                  {routingSummary}
                </div>
              ) : null}
              {hasRoutingLocks ? (
                <div className="mt-1.5">
                  <button
                    type="button"
                    onClick={() => clearRoutingLocks.mutate()}
                    disabled={clearRoutingLocks.isPending}
                    className="btn-ghost py-0.5 text-[9px]"
                    title="Clear preferred model, per-tier pins, and codegen model overrides"
                  >
                    {clearRoutingLocks.isPending ? "clearing locks…" : "clear routing locks"}
                  </button>
                </div>
              ) : null}
              {clearRoutingLocks.isError ? (
                <ErrorText className="mt-1 max-h-20 text-[9px]">
                  {String(clearRoutingLocks.error?.message || clearRoutingLocks.error)}
                </ErrorText>
              ) : null}
              <div className="mt-1 text-[9px] text-ash/80">{routingPolicy}</div>
              <div className="mt-1 text-[9px] text-ash/70">
                Automatic economy routes enforce the router's cheap price class. Explicit model pins remain unrestricted.
              </div>
            </div>
            {showRoutingDetails && routingRows.length ? (
              <>
                <div className="mt-2 border-t border-hairline pt-2">
                  <div className="text-[9px] text-ash/80">
                    Cost presets shown per tier: small (1k/1k), standard (5k/2k), heavy (20k/8k). {describeExampleWorkload(exampleWorkload)}
                  </div>
                </div>
                <div className="mt-2 overflow-hidden rounded border border-hairline/60">
                  {routingRows.map(({ key, row }) => (
                    <div key={key} className="p-2 border-b border-hairline/60 last:border-b-0">
                      {row}
                    </div>
                  ))}
                </div>
              </>
            ) : null}
            {!routingRows.length || showRoutingDetails ? null : (
              <div className="mt-1 break-words text-[9px] text-ash/80">
                pricing + routing lines shown per tier: open details
              </div>
            )}
            <SignalGrid
              label="Command deck"
              items={[
                { label: "mode", value: buildIntent.mode },
                { label: "model", value: buildIntent.model },
                { label: "reference", value: buildIntent.reference },
                { label: "fan-out", value: buildIntent.fanout },
              ]}
              right={
                <span title={assetState.title}>
                  <Pill tone={assetState.tone}>{assetState.label}</Pill>
                </span>
              }
              gridClassName="grid-cols-1"
              valueClassName="text-[11px]"
            />
          </Panel>
      </div>

      {fanout.cands.length > 0 || fanout.active ? (
        <Panel className="mb-6 overflow-hidden">
          <PanelHead
            label="Fan-out exploration"
            right={
              fanout.done ? (
                <span className="font-mono text-[11px] text-plasma">
                  winner <span className="text-bone">{fanout.done.winner || "—"}</span> · Δ +
                  {fanout.done.delta ?? 0}
                </span>
              ) : (
                <span className="font-mono text-[11px] text-ember">exploring…</span>
              )
            }
          />
          <div className="divide-y divide-hairline/60">
            {fanout.cands.map((c) => {
              const win = fanout.done && fanout.done.winner === c.candidate_id;
              return (
                <div
                  key={c.candidate_id}
                  className="flex items-center justify-between px-4 py-2"
                >
                  <span className="font-mono text-xs text-bone">
                    {win ? "★ " : ""}
                    {c.candidate_id}
                  </span>
                  <div className="flex items-center gap-3 font-mono text-[11px]">
                    <Pill tone={verdictTone(c.verdict)}>{c.verdict}</Pill>
                    <span className="text-ash">score {c.score ?? "—"}</span>
                    <span className={c.proof_passed ? "text-plasma" : "text-ember"}>
                      {c.proof_passed ? "proof ✓" : "proof ✕"}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </Panel>
      ) : null}

      <Panel className="mb-6 overflow-hidden">
        <PanelHead
          label="Forge line"
          right={
            <span className="font-mono text-[11px] text-ash">
              {running ? (
                <span className="text-ember">{running} igniting</span>
              ) : (
                <span className="text-plasma">{done}/{pipeline.length} cooled</span>
              )}
            </span>
          }
        />
        <div className="flex items-stretch gap-1 overflow-x-auto p-4">
          {pipeline.map((p, i) => (
            <React.Fragment key={p.stage}>
              <ForgeStage s={p} />
              {i < pipeline.length - 1 ? (
                <span
                  className={`mx-0.5 flex-shrink-0 self-center font-mono text-sm ${
                    p.state === "done" ? "text-plasma/60" : "text-ash/30"
                  }`}
                >
                  →
                </span>
              ) : null}
            </React.Fragment>
          ))}
        </div>
      </Panel>

      {/* The legible per-stage breakdown: agent, state, score, cost, gaps, time —
          all read from data the backend already emits on the event stream. */}
      <Panel className="mb-6 overflow-hidden">
        <PanelHead
          label="Stage ledger"
          right={
            <span className="font-mono text-[11px] text-ash">
              agent · score · cost · gaps
            </span>
          }
        />
        <StageLedger events={events} fallback={STAGES} />
      </Panel>

      {/* Cockpit: per-stage debug timeline + live artifact (Phase A) */}
      <div className="mb-6 grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel className="overflow-hidden">
          <PanelHead label="Stage debug" />
          <DebugTimeline events={events} />
        </Panel>
        <Panel className="overflow-hidden">
          <PanelHead label="Live preview" />
          <PreviewPanel events={events} />
          <PanelHead label="Files so far" />
          <FilesSoFar events={events} />
        </Panel>
      </div>

      <Panel>
        <PanelHead
          label="Recent builds"
          right={
            <div className="flex items-center gap-2">
              <span className="font-mono text-[11px] text-ash">
                {recentBuilds.length} total
              </span>
              <button
                type="button"
                onClick={() => cleanupCompletedBuilds.mutate()}
                disabled={
                  cleanupCompletedBuilds.isPending || terminalCleanupCandidates.length === 0
                }
                className="btn-ghost py-0.5 text-[10px] disabled:opacity-50"
                title="Delete completed/terminal builds from recent build history"
              >
                {cleanupCompletedBuilds.isPending
                  ? "Cleaning…"
                  : `Cleanup completed (${terminalCleanupCandidates.length})`}
              </button>
            </div>
          }
        />
        {approve.isError ? (
          <ErrorText className="mx-4 my-3 max-h-24">
            {String(approve.error?.message || approve.error)}
          </ErrorText>
        ) : null}
        {cancelBuild.isError ? (
          <ErrorText className="mx-4 my-3 max-h-24">
            {String(cancelBuild.error?.message || cancelBuild.error)}
          </ErrorText>
        ) : null}
        {cleanupBuild.isError ? (
          <ErrorText className="mx-4 my-3 max-h-24">
            {String(cleanupBuild.error?.message || cleanupBuild.error)}
          </ErrorText>
        ) : null}
        {cleanupCompletedBuilds.isError ? (
          <ErrorText className="mx-4 my-3 max-h-24">
            {String(cleanupCompletedBuilds.error?.message || cleanupCompletedBuilds.error)}
          </ErrorText>
        ) : null}
        {recentBuilds.length === 0 ? (
          <Empty icon="⬡">No builds yet. Submit a brief to fire the forge.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="eyebrow border-b border-hairline text-ash">
                  <th className="px-4 py-2 font-normal">Slug</th>
                  <th className="px-4 py-2 font-normal">Stack</th>
                  <th className="px-4 py-2 font-normal">Type</th>
                  <th className="px-4 py-2 font-normal">Engine</th>
                  <th className="px-4 py-2 font-normal">AI</th>
                  <th className="px-4 py-2 font-normal">Status</th>
                  <th className="px-4 py-2 font-normal">Score</th>
                  <th className="px-4 py-2 font-normal">LLM cost</th>
                  <th className="px-4 py-2 font-normal"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline/60">
                {recentBuilds.map((b) => {
                  const meta = buildMeta(b);
                  const ai = aiMeta(b);
                  const diagnostics = buildDiagnostics(b);
                  const fields = rebuildFields(b);
                  const buildKey = b.build_id || b.slug;
                  const active = isActiveBuild(b.status);
                  return (
                    <tr key={buildKey}>
                      <td className="px-4 py-2 font-mono text-xs text-bone">
                        {b.slug}
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.stack}</td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.appType}</td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">{meta.engine}</td>
                      <td className="px-4 py-2 font-mono text-[11px] text-ash">
                        <div
                          className="max-w-[13rem] truncate text-bone"
                          title={`${ai.profile} · backend setting ${ai.backend}`}
                        >
                          {ai.profile} · backend setting {ai.backend}
                        </div>
                        <div
                          className="max-w-[13rem] truncate text-ash/70"
                          title={`${ai.modelSource} · ${ai.model}`}
                        >
                          {ai.modelSource} · {ai.model}
                        </div>
                        <div
                          className="max-w-[13rem] truncate text-ash/70"
                          title={`prompts ${ai.promptCount} · stages ${ai.stageCount} · skills ${ai.skills} · recall ${ai.recall}`}
                        >
                          prompts {ai.promptCount} · stages {ai.stageCount}
                        </div>
                        <div
                          className="max-w-[13rem] truncate text-ash/70"
                          title={ai.costTitle}
                        >
                          {ai.costLabel} {ai.costQualifier} · stages {ai.stageCostLabel}
                        </div>
                        <div
                          className="max-w-[13rem] truncate text-ash/70"
                          title={diagnostics}
                        >
                          {diagnostics}
                        </div>
                      </td>
                      <td className="px-4 py-2">
                        <Pill tone={verdictTone(b.status)}>{b.status}</Pill>
                      </td>
                      <td className="px-4 py-2 font-mono text-xs text-ash">
                        {b.score ?? "—"}
                      </td>
                      <td
                        className="px-4 py-2 font-mono text-xs text-ash"
                        title={ai.costTitle}
                      >
                        <div>{ai.costLabel}</div>
                        <div className="max-w-[15rem] text-[9px] text-ash/70">
                          {ai.costQualifier}
                        </div>
                        <div className="max-w-[15rem] text-[9px] text-ash/70">
                          {ai.externalAssetLabel}
                        </div>
                      </td>
                      <td className="px-4 py-2 text-right">
                        {active ? (
                          <div className="flex items-center justify-end gap-2">
                            {b.approval_pending ? (
                              <>
                                <button
                                  onClick={() => {
                                    setPendingBuildId(buildKey);
                                    approve.mutate({
                                      build_id: buildKey,
                                      approved: true,
                                      reason: "",
                                    });
                                  }}
                                  disabled={approve.isPending && pendingBuildId === buildKey}
                                  className="btn-ember disabled:opacity-50"
                                  title="Approve the pending build gate"
                                >
                                  Approve
                                </button>
                                <button
                                  onClick={() => {
                                    setPendingBuildId(buildKey);
                                    approve.mutate({
                                      build_id: buildKey,
                                      approved: false,
                                      reason: "",
                                    });
                                  }}
                                  disabled={approve.isPending && pendingBuildId === buildKey}
                                  className="btn-ghost disabled:opacity-50"
                                  title="Reject the pending build gate"
                                >
                                  Reject
                                </button>
                              </>
                            ) : null}
                            <button
                              onClick={() => {
                                setPendingBuildId(buildKey);
                                cancelBuild.mutate({ build_id: buildKey });
                              }}
                              disabled={
                                cancelBuild.isPending &&
                                pendingBuildId === buildKey
                              }
                              className="btn-ghost disabled:opacity-50"
                              title="Cancel this build"
                            >
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <div className="flex items-center justify-end gap-2">
                            <button
                              onClick={() => loadRebuildVariant(b)}
                              disabled={!fields.brief.trim()}
                              className="btn-ghost disabled:opacity-50"
                              title={
                                fields.brief.trim()
                                  ? "Load this build into the form for an editable rebuild variant"
                                  : "No recoverable brief"
                              }
                            >
                              Rebuild
                            </button>
                            <button
                              onClick={() => {
                                setPendingBuildId(buildKey);
                                cleanupBuild.mutate({ build_id: buildKey });
                              }}
                              disabled={
                                !isTerminalBuild(b.status) ||
                                (cleanupBuild.isPending &&
                                pendingBuildId === buildKey)
                              }
                              className="btn-ghost disabled:opacity-50"
                              title={
                                isTerminalBuild(b.status)
                                  ? "Delete this build from recent history"
                                  : "Only completed builds can be cleaned up"
                              }
                            >
                              Delete
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
