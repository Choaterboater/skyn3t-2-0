import React, { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, queryFn, apiPost, saveAuthToken } from "../api.js";
import {
  DEPLOY_PROVIDERS,
  deployProviderDetail,
  deployProviderConfigured,
  withDeployCredentialStatus,
} from "../deploySettings.js";
import {
  describeExampleWorkload,
  describeModelValue,
  findModelValue,
  formatModelOption,
} from "../modelValue.js";
import {
  BACKEND_OPTIONS,
  CODEGEN_CLI_OPTIONS,
  backendOption,
  backendOptionLabel,
  cliAccountBillingText,
  cliBackendStatus,
  cliProviderStatus,
} from "../cliBackends.js";
import {
  PageHeader,
  Panel,
  PanelHead,
  Pill,
  Empty,
  SignalGrid,
} from "../components/ui.jsx";

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between border-b border-hairline/60 px-4 py-2.5 last:border-b-0">
      <span className="font-mono text-[11px] uppercase tracking-eyebrow text-ash">{label}</span>
      <span className="font-mono text-xs text-bone">{String(value)}</span>
    </div>
  );
}

const PROVIDERS = ["openrouter", "anthropic", "openai", "kimi"];
const MODEL_TIERS = ["cheap", "ui", "backend", "strong", "docs"];
const CHANNELS = ["telegram", "discord", "slack"];
const APP_TYPES = ["auto", "product_app", "dashboard", "landing_page", "crud_app", "saas_product", "game", "api_service", "developer_tool", "data_viz", "mobile_app", "desktop_app"];
const ENGINES = ["auto", "dom", "browser_native", "phaser", "godot", "bevy", "raylib", "expo", "tauri", "server", "python", "none"];
const SETTINGS_SECTIONS = [
  ["backend", "Backend"],
  ["routing", "Routing"],
  ["metadata", "Build"],
  ["keys", "Keys"],
  ["github", "GitHub"],
  ["deploy", "Deploy"],
  ["images", "Images"],
  ["visual", "Visual"],
  ["improve", "Improve"],
  ["gates", "Gates"],
  ["messaging", "Messaging"],
  ["auth", "Auth"],
  ["runtime", "Runtime"],
];

export default function Settings() {
  const queryClient = useQueryClient();
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: queryFn("/settings"),
    retry: 0,
  });
  const { data, error } = settings;

  const secrets = useQuery({
    queryKey: ["llm-secrets"],
    queryFn: queryFn("/llm/secrets"),
    retry: 0,
    refetchInterval: 4000,
  });

  const llmBackends = useQuery({
    queryKey: ["llm-backends"],
    queryFn: queryFn("/llm/backends"),
    retry: 0,
    refetchInterval: 4000,
  });

  const deploySettings = useQuery({
    queryKey: ["deploy-settings"],
    queryFn: queryFn("/settings/deploy"),
    retry: 0,
  });

  const integrations = useQuery({
    queryKey: ["integrations"],
    queryFn: queryFn("/integrations"),
    retry: 0,
  });

  // Every end-of-build verification gate, straight from the registry — a new
  // gate shows up here with zero UI changes.
  const gates = useQuery({
    queryKey: ["gates"],
    queryFn: queryFn("/gates"),
    retry: 0,
  });

  const [token, setToken] = useState(
    typeof localStorage !== "undefined"
      ? localStorage.getItem("skyn3t_token") || ""
      : ""
  );
  const [saved, setSaved] = useState(false);

  const [provider, setProvider] = useState("openrouter");
  const [key, setKey] = useState("");
  const [backendMsg, setBackendMsg] = useState("");
  const [keyMsg, setKeyMsg] = useState("");
  const [routingMsg, setRoutingMsg] = useState("");
  const [codegenCliProvider, setCodegenCliProvider] = useState("");
  const [codegenCliModel, setCodegenCliModel] = useState("");
  const [openrouterCodegenModel, setOpenrouterCodegenModel] = useState("");
  const [modelPins, setModelPins] = useState({
    cheap: "",
    ui: "",
    backend: "",
    strong: "",
    docs: "",
  });

  const [ghToken, setGhToken] = useState("");
  const [ghMsg, setGhMsg] = useState("");

  const [deployProvider, setDeployProvider] = useState("fly");
  const [deployToken, setDeployToken] = useState("");
  const [deployMsg, setDeployMsg] = useState("");

  const [repToken, setRepToken] = useState("");
  const [repModel, setRepModel] = useState("");
  const [repMsg, setRepMsg] = useState("");
  const [visualMsg, setVisualMsg] = useState("");
  const [agenticMsg, setAgenticMsg] = useState("");
  const [gateMsg, setGateMsg] = useState("");

  // Model dropdown: the LIVE OpenRouter list (auto-updates with the newest
  // models) + the currently-pinned model (empty = auto / smart routing).
  const models = useQuery({
    queryKey: ["models"],
    queryFn: queryFn("/models"),
    retry: 0,
  });
  const [modelCatalogActive, setModelCatalogActive] = useState(false);
  const [modelCatalogQuery, setModelCatalogQuery] = useState("");
  const [debouncedModelCatalogQuery, setDebouncedModelCatalogQuery] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(
      () => setDebouncedModelCatalogQuery(modelCatalogQuery.trim()),
      180,
    );
    return () => window.clearTimeout(timer);
  }, [modelCatalogQuery]);
  const pricedModels = useQuery({
    queryKey: ["models", "settings-catalog", debouncedModelCatalogQuery],
    queryFn: () => {
      const params = new URLSearchParams({
        limit: "50",
        sort: "price",
        order: "asc",
      });
      if (debouncedModelCatalogQuery) params.set("q", debouncedModelCatalogQuery);
      return apiFetch(`/models/catalog?${params.toString()}`);
    },
    enabled: modelCatalogActive,
    retry: 0,
  });
  const [model, setModel] = useState("");
  const [modelMsg, setModelMsg] = useState("");
  const normalizeModelId = (value) => value.replace(/\s+/g, "").trim();
  const [modelValidation, setModelValidation] = useState(null);
  useEffect(() => {
    const cur = secrets.data?.preferred_model;
    if (cur !== undefined) {
      const normalized = normalizeModelId(cur || "");
      setModel(normalized);
      if (secrets.data) {
        void refreshModelValidation(normalized);
      }
    }
  }, [secrets.data?.preferred_model]);

  async function refreshModelValidation(nextModel) {
    const normalized = normalizeModelId(nextModel);
    if (!normalized) {
      setModelValidation({
        model: "",
        status: "auto",
        available: true,
      });
      return;
    }
    try {
      const resolved = await apiFetch(`/models/resolve?model=${encodeURIComponent(normalized)}`);
      setModelValidation(resolved);
    } catch (e) {
      setModelValidation({
        model: normalized,
        status: "unknown",
        available: false,
        note: String(e?.message || e),
      });
    }
  }

  async function saveModel(m) {
    const normalized = normalizeModelId(m);
    setModel(normalized);
    try {
      await apiPost("/settings/model", { model: normalized });
      await refreshModelValidation(normalized);
      queryClient.setQueryData(["llm-secrets"], (old) => ({
        ...(old || {}),
        preferred_model: normalized,
      }));
      setModelMsg(normalized ? `pinned → ${normalized}` : "auto — smart routing");
      secrets.refetch();
    } catch (e) {
      setModelMsg(String(e.message));
    }
  }

  const [appTypeOverride, setAppTypeOverride] = useState("auto");
  const [engineOverride, setEngineOverride] = useState("auto");
  const [metaMsg, setMetaMsg] = useState("");

  const [channel, setChannel] = useState("telegram");
  const [chToken, setChToken] = useState("");
  const [chTarget, setChTarget] = useState("");
  const [chMsg, setChMsg] = useState("");

  useEffect(() => {
    if (!data) return;
    setAppTypeOverride(data.app_type_override || "auto");
    setEngineOverride(data.engine_override || "auto");
  }, [data]);

  useEffect(() => {
    const d = secrets.data;
    if (!d) return;
    setCodegenCliProvider(d.codegen_cli_provider || "");
    setCodegenCliModel(d.codegen_cli_model || "");
    setOpenrouterCodegenModel(d.openrouter_codegen_model || "");
    setModelPins({
      cheap: d.model_pins?.cheap || "",
      ui: d.model_pins?.ui || "",
      backend: d.model_pins?.backend || "",
      strong: d.model_pins?.strong || "",
      docs: d.model_pins?.docs || "",
    });
  }, [secrets.data]);

  function saveToken() {
    const next = token.trim();
    setToken(next);
    saveAuthToken(next);
    void queryClient.invalidateQueries();
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }
  async function saveKey() {
    try {
      const r = await apiPost("/llm/key", { provider, key });
      setKey("");
      setKeyMsg(`${provider}: ${r.configured ? "saved" : "cleared"} → backend ${r.backend}`);
      queryClient.setQueryData(["llm-secrets"], (old) => ({
        ...(old || {}),
        providers: {
          ...((old && old.providers) || {}),
          [provider]: !!r.configured,
        },
        backend: r.backend || (old && old.backend),
        routing: r.routing || (old && old.routing),
      }));
      secrets.refetch();
      if (provider === "openrouter") {
        models.refetch();
        if (modelCatalogActive) pricedModels.refetch();
      }
    } catch (e) {
      setKeyMsg(String(e.message));
    }
  }

  async function saveGithub() {
    try {
      const r = await apiPost("/settings/github", { token: ghToken });
      setGhToken("");
      setGhMsg(r.configured ? "saved → RepoScout now searches GitHub" : "cleared");
      secrets.refetch();
    } catch (e) {
      setGhMsg(String(e.message));
    }
  }

  async function saveDeployCredential(clear = false) {
    try {
      const r = await apiPost("/settings/deploy/credential", {
        provider: deployProvider,
        token: clear ? "" : deployToken,
      });
      setDeployToken("");
      setDeployMsg(`${deployProvider}: ${r.configured ? "configured" : "cleared"}`);
      queryClient.setQueryData(["deploy-settings"], (old) =>
        withDeployCredentialStatus(old, deployProvider, r.configured)
      );
      void deploySettings.refetch();
    } catch (e) {
      setDeployMsg(String(e.message));
    }
  }

  async function saveAllowRemoteDeploy(enabled) {
    try {
      const r = await apiPost("/settings/deploy/allow_remote", { enabled });
      queryClient.setQueryData(["deploy-settings"], (old) => ({
        ...(old || {}),
        allow_remote_deploy: !!r.allow_remote_deploy,
      }));
      setDeployMsg(`remote deploy ${r.allow_remote_deploy ? "enabled" : "disabled"}`);
      settings.refetch();
      void deploySettings.refetch();
    } catch (e) {
      setDeployMsg(String(e.message));
    }
  }

  async function saveReplicate() {
    try {
      const r = await apiPost("/settings/replicate", {
        token: repToken,
        model: repModel,
      });
      setRepToken("");
      setRepMsg(
        r.configured
          ? `saved → image generation available (model ${r.model || "default"})`
          : "cleared"
      );
      queryClient.setQueryData(["llm-secrets"], (old) => ({
        ...(old || {}),
        replicate: !!r.configured,
        replicate_model: r.model || repModel.trim() || ((old && old.replicate_model) || ""),
      }));
      secrets.refetch();
    } catch (e) {
      setRepMsg(String(e.message));
    }
  }

  async function saveAssetGen(enabled) {
    try {
      await apiPost("/settings/asset_gen", { enabled });
      setRepMsg(
        enabled
          ? "asset generation ON → new builds generate real images (Replicate billing applies)"
          : "asset generation off"
      );
      secrets.refetch();
    } catch (e) {
      setRepMsg(String(e.message));
    }
  }

  async function saveGate(gate, enabled) {
    try {
      await apiPost("/settings/gate", { gate, enabled });
      setGateMsg(`${gate} ${enabled ? "enabled" : "disabled"} for future builds`);
      gates.refetch();
    } catch (e) {
      setGateMsg(String(e?.message || e));
    }
  }

  async function saveVisualSelfHeal(enabled) {
    try {
      const r = await apiPost("/settings/visual_self_heal", { enabled });
      setVisualMsg(
        r.visual_self_heal
          ? `visual self-heal ON → up to ${r.visual_self_heal_max_rounds} rendered repair round(s)`
          : "visual self-heal off"
      );
      settings.refetch();
      secrets.refetch();
    } catch (e) {
      setVisualMsg(String(e.message));
    }
  }

  async function saveImproveAgentic(enabled) {
    try {
      const r = await apiPost("/settings/improve_agentic", { enabled });
      setAgenticMsg(
        r.improve_agentic
          ? `agentic improve ON → multi-file goals, up to ${r.improve_agentic_timeout}s per session`
          : "agentic improve off → classic single-file rewrites only"
      );
      settings.refetch();
    } catch (e) {
      setAgenticMsg(String(e.message));
    }
  }

  async function saveBuildMetadata() {
    try {
      const r = await apiPost("/settings/build_metadata", {
        app_type: appTypeOverride || "auto",
        engine: engineOverride || "auto",
      });
      setMetaMsg(`build metadata -> app ${r.app_type_override}, engine ${r.engine_override}`);
    } catch (e) {
      setMetaMsg(String(e.message));
    }
  }

  async function pickBackend(b) {
    try {
      const r = await apiPost("/llm/backend", { backend: b });
      const state = r.routing?.state ? ` (${r.routing.state})` : "";
      setBackendMsg(`saved globally -> requested ${r.requested || b}; active ${r.active}${state}`);
      queryClient.setQueryData(["llm-secrets"], (old) => ({
        ...(old || {}),
        backend_pref: r.requested || b,
        backend: r.active,
        routing: r.routing || old?.routing,
      }));
      void secrets.refetch();
      void llmBackends.refetch();
    } catch (e) {
      setBackendMsg(String(e.message));
    }
  }

  async function saveRouting() {
    try {
      const r = await apiPost("/llm/routing", {
        codegen_cli_provider: codegenCliProvider,
        codegen_cli_model: codegenCliModel,
        openrouter_codegen_model: normalizeModelId(openrouterCodegenModel),
        model_pins: {
          cheap: normalizeModelId(modelPins.cheap),
          ui: normalizeModelId(modelPins.ui),
          backend: normalizeModelId(modelPins.backend),
          strong: normalizeModelId(modelPins.strong),
          docs: normalizeModelId(modelPins.docs),
        },
      });
      setRoutingMsg(`saved → codegen ${r.routing?.codegen?.backend || "active backend"}`);
      secrets.refetch();
    } catch (e) {
      setRoutingMsg(String(e.message));
    }
  }

  async function saveChannel() {
    try {
      const r = await apiPost("/integrations/credential", {
        channel,
        token: chToken,
        target: chTarget,
      });
      setChToken("");
      setChTarget("");
      setChMsg(`${channel}: ${r?.configured === false ? "cleared" : "saved"}`);
      integrations.refetch();
    } catch (e) {
      setChMsg(String(e.message));
    }
  }

  async function controlListener(action) {
    try {
      const r = await apiPost("/integrations/listener", { action });
      if (action === "test") setChMsg(`test sent to ${r.sent ?? 0} channel(s)`);
      else if (r.error) setChMsg(r.error);
      else setChMsg(`bot ${r.running ? "started — listening on Telegram" : "stopped"}`);
      integrations.refetch();
    } catch (e) {
      setChMsg(String(e.message));
    }
  }

  const flags = data
    ? {
        free_only: data.free_only,
        no_claude: data.no_claude,
        execution_backend: data.execution_backend,
        autonomous_builds: data.autonomous_builds,
        approval_gates: data.approval_gates,
        visual_self_heal: data.visual_self_heal,
        visual_self_heal_max_rounds: data.visual_self_heal_max_rounds,
        per_build_usd_cap: data.per_build_usd_cap,
        daily_usd_cap: data.daily_usd_cap,
        daily_token_cap: data.daily_token_cap,
        autonomous_daily_build_cap: data.autonomous_daily_build_cap,
      }
    : null;

  const active = secrets.data?.backend;
  const routing = secrets.data?.routing || {};
  const codegen = routing.codegen || {};
  const requestedBackend = secrets.data?.backend_pref || routing.requested || "auto";
  const selectedBackendOption = backendOption(requestedBackend);
  const selectedBackendStatus = cliBackendStatus(llmBackends.data, requestedBackend);
  const selectedBackendBilling = cliAccountBillingText(requestedBackend);
  const providerConfigured = !!secrets.data?.providers?.[provider];
  const openrouterConfigured =
    !!secrets.data?.providers?.openrouter || !!routing.openrouter_configured;
  const openrouterRequired = requestedBackend === "openrouter";
  const openrouterStateText = openrouterConfigured
    ? "API key configured"
    : openrouterRequired
      ? "required by selected backend - missing"
      : "optional - no API key";
  const selectedDeployConfigured = deployProviderConfigured(
    deploySettings.data,
    deployProvider
  );
  const selectedDeployDetail = deployProviderDetail(
    deploySettings.data,
    deployProvider,
  );
  const chData = integrations.data?.channels || {};
  const openrouterModels = models.data?.models || [];
  const openrouterModelItems = Array.isArray(pricedModels.data?.items)
    ? pricedModels.data.items
    : [];
  const primaryModelChoices = openrouterModelItems;
  const codegenModelChoices = openrouterModelItems;
  const selectedPrimaryValue = findModelValue(openrouterModelItems, model);
  const selectedCodegenValue = findModelValue(openrouterModelItems, openrouterCodegenModel);
  const selectedTierValues = MODEL_TIERS.map((tier) => ({
    tier,
    value: findModelValue(openrouterModelItems, modelPins[tier]),
  })).filter((entry) => entry.value);
  const routingCockpit = [
    { label: "requested backend", value: routing.requested || "auto" },
    { label: "active route", value: routing.active || active || "stub" },
    { label: "primary model", value: model || "auto · learned routing" },
    { label: "codegen path", value: codegen.reason || "follows active backend" },
  ];

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Console"
        title="Settings"
        sub="LLM backend & keys, messaging channels, runtime config, and dashboard auth token."
        actions={
          <span className="badge border-hairline text-ash">
            backend · <span className="ml-1 text-ember">{active || "…"}</span>
          </span>
        }
      />

      <div className="space-y-6">
        <nav className="panel flex flex-wrap gap-2 p-3" aria-label="Settings sections">
          {SETTINGS_SECTIONS.map(([id, label]) => (
            <a key={id} href={`#${id}`} className="badge border-hairline text-ash hover:border-ember/40 hover:text-bone">
              {label}
            </a>
          ))}
        </nav>

        <Panel id="backend">
          <PanelHead
            label="LLM backend"
            right={
              <Pill tone={active ? "plasma" : "ash"}>active · {active || "…"}</Pill>
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              This choice is persisted for every future Foundry run. CLI backends use
              the installed command and its signed-in provider account. OpenRouter is
              optional and only works when its API key is configured.
            </p>
            <p className="mb-4 text-[11px] text-ash/80">
              An explicitly selected CLI never falls back to OpenRouter. If its command
              is unavailable, the backend reports <span className="font-mono text-bone">cli_missing</span>{" "}
              and uses the offline stub.
            </p>
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              {BACKEND_OPTIONS.map((option) => {
                const sel = requestedBackend === option.id;
                const status = cliBackendStatus(llmBackends.data, option.id);
                const unavailable = option.kind === "cli" && status.available !== true;
                return (
                  <button
                    key={option.id}
                    type="button"
                    onClick={() => pickBackend(option.id)}
                    disabled={unavailable && !sel}
                    aria-pressed={sel}
                    title={
                      option.kind === "cli"
                        ? `${backendOptionLabel(option, llmBackends.data)}. ${cliAccountBillingText(option.id)}`
                        : `Persist ${option.label} as the global Foundry backend`
                    }
                    className={`min-h-10 rounded border px-3 py-2 text-left font-mono text-[11px] transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
                      sel
                        ? "border-ember/60 bg-ember/10 text-ember"
                        : "border-hairline text-ash hover:border-ember/40 hover:text-bone"
                    }`}
                  >
                    {backendOptionLabel(option, llmBackends.data)}
                  </button>
                );
              })}
            </div>
            {selectedBackendOption?.kind === "cli" ? (
              <div className="mt-3 border-l-2 border-hairline pl-3 text-[11px] text-ash">
                <p className={selectedBackendStatus.available ? "text-plasma" : "text-ember"}>
                  {selectedBackendStatus.available
                    ? `${selectedBackendOption.label} command available${
                        selectedBackendStatus.detail?.path
                          ? ` at ${selectedBackendStatus.detail.path}`
                          : ""
                      }.`
                    : selectedBackendStatus.checked
                      ? `${selectedBackendOption.label} command was not found on PATH.`
                      : `${selectedBackendOption.label} availability is being checked.`}
                </p>
                <p className="mt-1">{selectedBackendBilling}</p>
              </div>
            ) : null}
            {backendMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{backendMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="routing">
          <PanelHead
            label="Model routing"
            right={
              <Pill tone={routing.state === "ready" ? "plasma" : "ash"}>
                {routing.state || "unknown"}
              </Pill>
            }
          />
          <div className="p-4">
            <SignalGrid
              label="Routing cockpit"
              items={routingCockpit}
              className="mb-4"
              gridClassName="md:grid-cols-4"
              valueClassName="min-h-[2.5rem]"
            />
            <div className="mb-4 overflow-hidden rounded border border-hairline/60">
              <Row label="requested" value={routing.requested || "auto"} />
              <Row label="active" value={routing.active || active || "stub"} />
              <Row
                label="OpenRouter"
                value={openrouterStateText}
              />
              <Row label="reason" value={routing.reason || "ready"} />
              <Row label="codegen" value={codegen.reason || "follows active backend"} />
            </div>
            <div className="mb-4 border-b border-hairline pb-4">
              <p className="mb-2 text-sm text-ash">
                Primary OpenRouter model.{" "}
                <span className="font-mono text-bone">auto</span> lets the router
                pick per task; pin one here for OpenRouter calls that are not using
                a more specific override. The OpenRouter codegen model below wins for
                whole-app builds when it is set.
              </p>
              <p className="mb-2 text-[11px] text-ash/75">
                {describeExampleWorkload(pricedModels.data?.example_workload)} High-price choices remain selectable; benchmark-near cheaper peers are shown when OpenRouter publishes enough comparison data.
              </p>
              <div className="flex flex-wrap items-center gap-2">
                  <div className="min-w-0 flex-1 sm:min-w-[16rem]">
                    <input
                      type="text"
                      aria-label="Primary OpenRouter model"
                      list="preferred-models"
                      value={model}
                      onChange={(e) => {
                        setModel(e.target.value);
                        setModelCatalogQuery(e.target.value);
                      }}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        saveModel(model);
                      }
                    }}
                    onBlur={() => saveModel(model)}
                    onFocus={() => {
                      setModelCatalogActive(true);
                      setModelCatalogQuery(model);
                      refreshModelValidation(model);
                    }}
                    placeholder="auto — smart routing"
                    className="field"
                  />
                  <datalist id="preferred-models">
                    <option value="">auto — smart routing</option>
                    {primaryModelChoices.map((item) => (
                      <option
                        key={item.id}
                        value={item.id}
                        label={formatModelOption(item)}
                      />
                    ))}
                  </datalist>
                </div>
                <button className="btn-ghost" onClick={() => saveModel(model)}>
                  Set
                </button>
                <button
                  onClick={() => {
                    models.refetch();
                    if (modelCatalogActive) pricedModels.refetch();
                  }}
                  className="btn-ghost"
                >
                  Refresh models
                </button>
                <span className="font-mono text-[11px] text-ash/60">
                  {models.isLoading
                    ? "loading..."
                    : models.data?.note
                      ? models.data.note
                      : `${(models.data?.models || []).length} models`}
                </span>
              </div>
              {modelValidation ? (
                <p
                  className={`mt-1 font-mono text-[11px] ${
                    modelValidation.available ? "text-ash/60" : "text-ember"
                  }`}
                >
                  {modelValidation.status === "auto"
                    ? "model mode: auto (learned router)"
                    : modelValidation.available
                      ? "model found in OpenRouter catalog"
                      : `model not found in catalog${modelValidation.note ? ` — ${modelValidation.note}` : ""}`}
                  {modelValidation.status !== "auto" &&
                  !modelValidation.available &&
                  Array.isArray(modelValidation.suggestions)
                    ? modelValidation.suggestions.length
                      ? `; try: ${modelValidation.suggestions.join(", ")}`
                      : ""
                    : ""}
                </p>
              ) : null}
              {selectedPrimaryValue ? (
                <p className="mt-1 break-words font-mono text-[11px] text-ash/75">
                  {describeModelValue(selectedPrimaryValue, "primary")}
                </p>
              ) : null}
              {modelMsg ? (
                <p className="mt-2 font-mono text-[11px] text-plasma">{modelMsg}</p>
              ) : null}
            </div>
            <div className="mt-4 border-t border-hairline pt-4">
              <p className="mb-2 text-sm text-ash">
                Advanced codegen-only override. This is separate from the global
                Foundry backend above and only changes the CodeAgent route.
              </p>
              <p className="mb-3 text-[11px] text-ash/80">
                A CLI override uses that CLI&apos;s signed-in account. Usage limits and
                billing follow the provider account&apos;s plan or subscription. Unavailable
                commands cannot be selected.
              </p>
              <div className="flex flex-wrap gap-2">
              <select
                aria-label="CLI codegen provider"
                value={codegenCliProvider}
                onChange={(e) => setCodegenCliProvider(e.target.value)}
                className="field max-w-[16rem]"
              >
                {CODEGEN_CLI_OPTIONS.map((option) => {
                  const status = cliProviderStatus(llmBackends.data, option.provider);
                  const unavailable = option.kind === "cli" && status.available !== true;
                  return (
                    <option
                      key={option.provider || "none"}
                      value={option.provider}
                      disabled={unavailable && codegenCliProvider !== option.provider}
                    >
                      {option.kind === "cli"
                        ? `${option.label} - ${status.availabilityLabel}`
                        : option.label}
                    </option>
                  );
                })}
              </select>
              <input
                type="text"
                className="field min-w-[10rem] flex-1"
                aria-label="CLI codegen model"
                placeholder="CLI model (optional)"
                value={codegenCliModel}
                onChange={(e) => setCodegenCliModel(e.target.value)}
                disabled={!codegenCliProvider}
                title="Optional model flag for the selected codegen CLI"
              />
              <div className="min-w-0 flex-1 sm:min-w-[14rem]">
                <input
                  type="text"
                  className="field"
                  aria-label="OpenRouter codegen model"
                  list="openrouter-codegen-models"
                  value={openrouterCodegenModel}
                  onChange={(e) => {
                    setOpenrouterCodegenModel(e.target.value);
                    setModelCatalogQuery(e.target.value);
                  }}
                  onFocus={() => {
                    setModelCatalogActive(true);
                    setModelCatalogQuery(openrouterCodegenModel);
                  }}
                  title="OpenRouter codegen model; overrides primary for whole-app builds"
                  placeholder="OpenRouter codegen · auto"
                />
                <datalist id="openrouter-codegen-models">
                  <option value="" />
                  {codegenModelChoices.map((item) => (
                    <option
                      key={item.id}
                      value={item.id}
                      label={formatModelOption(item)}
                    />
                  ))}
                </datalist>
              </div>
              </div>
            </div>
            {selectedCodegenValue ? (
              <p className="mt-2 break-words font-mono text-[10px] text-ash/75">
                {describeModelValue(selectedCodegenValue, "whole-app codegen")}
              </p>
            ) : null}
            <div className="mt-3 grid gap-2 md:grid-cols-5">
              {MODEL_TIERS.map((tier) => (
                <input
                  key={tier}
                  type="text"
                  className="field min-w-0"
                  placeholder={`${tier} model`}
                  aria-label={tier + " model"}
                  list="routing-tier-models"
                  value={modelPins[tier] || ""}
                  onChange={(e) => {
                    setModelPins((prev) => ({ ...prev, [tier]: e.target.value }));
                    setModelCatalogQuery(e.target.value);
                  }}
                  onFocus={() => {
                    setModelCatalogActive(true);
                    setModelCatalogQuery(modelPins[tier] || "");
                  }}
                />
              ))}
            </div>
            <datalist id="routing-tier-models">
              {openrouterModelItems.map((item) => (
                <option
                  key={item.id}
                  value={item.id}
                  label={formatModelOption(item)}
                />
              ))}
            </datalist>
            {selectedTierValues.length ? (
              <div className="mt-2 space-y-1">
                {selectedTierValues.map(({ tier, value }) => (
                  <p key={tier} className="break-words font-mono text-[10px] text-ash/75">
                    {describeModelValue(value, tier === "cheap" ? "economy-task role" : `${tier} role`)}
                  </p>
                ))}
              </div>
            ) : null}
            <button onClick={saveRouting} className="btn-ember mt-3">
              Save routing
            </button>
            {routingMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{routingMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="metadata">
          <PanelHead label="Build metadata defaults" />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              Leave these on <span className="font-mono text-bone">auto</span> so SkyN3t
              infers app type and engine from the brief + selected stack. Pick an override
              only when you want future builds labeled a specific way without hardcoding the
              scaffold.
            </p>
            <div className="flex flex-wrap gap-2">
              <select
                aria-label="Default app type"
                value={appTypeOverride}
                onChange={(e) => setAppTypeOverride(e.target.value)}
                className="field max-w-[14rem]"
              >
                {APP_TYPES.map((v) => (
                  <option key={v} value={v}>
                    app · {v}
                  </option>
                ))}
              </select>
              <select
                aria-label="Default engine"
                value={engineOverride}
                onChange={(e) => setEngineOverride(e.target.value)}
                className="field max-w-[14rem]"
              >
                {ENGINES.map((v) => (
                  <option key={v} value={v}>
                    engine · {v}
                  </option>
                ))}
              </select>
              <button onClick={saveBuildMetadata} className="btn-ember">
                Save defaults
              </button>
            </div>
            {metaMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{metaMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="keys">
          <PanelHead
            label="API key"
            right={
              <Pill tone={providerConfigured ? "plasma" : "ash"}>
                {providerConfigured
                  ? `${provider} configured ✓`
                  : provider === "openrouter"
                    ? openrouterRequired
                      ? "openrouter required - no key"
                      : "openrouter optional - no key"
                    : `${provider} no key`}
              </Pill>
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              Stored in the server&apos;s <code className="font-mono text-bone">.env</code>.
              OpenRouter is optional when a CLI backend is selected. Add its key only
              when you intend to use the OpenRouter route;{" "}
              <span className="font-mono text-bone">auto</span> may resolve to that route
              after a key is configured.
            </p>
            <div className="mb-3">
              <Row
                label="OpenRouter"
                value={openrouterConfigured ? "API key configured ✓" : openrouterStateText}
              />
            </div>
            <div className="flex flex-wrap gap-2">
              <select
                aria-label="API key provider"
                value={provider}
                onChange={(e) => setProvider(e.target.value)}
                className="field max-w-[12rem]"
              >
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                    {secrets.data?.providers?.[p] ? " ✓" : ""}
                  </option>
                ))}
              </select>
              <input
                type="password"
                className="field min-w-[12rem] flex-1"
                placeholder={`${provider} API key`}
                aria-label={provider + " API key"}
                autoComplete="off"
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
              <button onClick={saveKey} className="btn-ember">
                Save key
              </button>
            </div>
            {keyMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{keyMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="github">
          <PanelHead
            label="GitHub token"
            right={
              <Pill tone={secrets.data?.github ? "plasma" : "ash"}>
                {secrets.data?.github ? "configured ✓" : "not set"}
              </Pill>
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              Stored in the server&apos;s <code className="font-mono text-bone">.env</code> as{" "}
              <span className="font-mono text-bone">SKYN3T_GITHUB_TOKEN</span>. Lets the
              Cortex <span className="font-mono text-bone">RepoScout</span> search GitHub for
              real (authenticated, higher rate limit) and ingest repos into the knowledge
              base — without a token it falls back to a small seed list.
            </p>
            <div className="flex flex-wrap gap-2">
              <input
                type="password"
                className="field min-w-[12rem] flex-1"
                placeholder="GitHub token (ghp_… / gho_…)"
                aria-label="GitHub token"
                autoComplete="off"
                value={ghToken}
                onChange={(e) => setGhToken(e.target.value)}
              />
              <button onClick={saveGithub} className="btn-ember">
                Save token
              </button>
            </div>
            {ghMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{ghMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="deploy">
          <PanelHead
            label="Production deploy"
            right={
              deploySettings.error ? (
                <Pill tone="ember">unreachable</Pill>
              ) : (
                <Pill tone={selectedDeployConfigured ? "plasma" : "ash"}>
                  {deployProvider} · {selectedDeployConfigured ? "configured ✓" : "not set"}
                </Pill>
              )
            }
          />
          <div className="p-4">
            <div className="flex flex-wrap gap-2">
              <select
                aria-label="Deploy provider"
                value={deployProvider}
                onChange={(e) => {
                  setDeployProvider(e.target.value);
                  setDeployToken("");
                  setDeployMsg("");
                }}
                className="field max-w-[12rem]"
              >
                {DEPLOY_PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                    {deployProviderConfigured(deploySettings.data, p) ? " ✓" : ""}
                  </option>
                ))}
              </select>
              <input
                type="password"
                className="field min-w-[12rem] flex-1"
                placeholder={`${deployProvider} credential`}
                aria-label={deployProvider + " deploy credential"}
                autoComplete="off"
                value={deployToken}
                onChange={(e) => setDeployToken(e.target.value)}
              />
              <button
                onClick={() => saveDeployCredential(false)}
                className="btn-ember"
                disabled={!deployToken.trim()}
              >
                Save credential
              </button>
              <button
                onClick={() => saveDeployCredential(true)}
                className="btn-ghost"
              >
                Clear
              </button>
            </div>
            <div className="mt-3 grid gap-1 font-mono text-[10px] text-ash sm:grid-cols-3">
              <span>
                credential {selectedDeployDetail.configured ? "ready" : "missing"}
              </span>
              <span>
                {selectedDeployDetail.cli || deployProvider} CLI {selectedDeployDetail.cli_available ? "installed" : "missing"}
              </span>
              <span className={selectedDeployDetail.ready ? "text-plasma" : "text-ash/70"}>
                remote path {selectedDeployDetail.ready ? "ready" : "not ready"}
              </span>
            </div>
            <label className="mt-4 flex items-center gap-2 text-sm text-bone">
              <input
                type="checkbox"
                className="h-4 w-4 accent-ember"
                checked={!!deploySettings.data?.allow_remote_deploy}
                onChange={(e) => saveAllowRemoteDeploy(e.target.checked)}
              />
              <span>
                Allow remote deploy
                {deploySettings.data?.allow_remote_deploy ? (
                  <span className="text-ember"> · ON</span>
                ) : (
                  <span className="text-ash"> · off</span>
                )}
              </span>
            </label>
            {deployMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{deployMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="images">
          <PanelHead
            label="Replicate (image generation)"
            right={
              <Pill tone={secrets.data?.replicate ? "plasma" : "ash"}>
                {secrets.data?.replicate ? "configured ✓" : "not set"}
              </Pill>
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              Stored in the server&apos;s <code className="font-mono text-bone">.env</code> as{" "}
              <span className="font-mono text-bone">SKYN3T_REPLICATE_API_TOKEN</span>. Lets a
              build generate <em>real</em> images (e.g. a kids coloring app&apos;s animal
              line-art) instead of crappy placeholder art. Asset generation also requires{" "}
              <span className="font-mono text-bone">asset_gen</span> on
              {secrets.data?.replicate && !secrets.data?.asset_gen ? (
                <span className="text-ember"> — currently OFF, so assets won&apos;t generate yet</span>
              ) : null}
              . Default model{" "}
              <span className="font-mono text-bone">
                {secrets.data?.replicate_model || "black-forest-labs/flux-schnell"}
              </span>{" "}
              (overridable below). No token → image-gen is skipped; it never blocks a build.
            </p>
            <div className="flex flex-wrap gap-2">
              <input
                type="password"
                className="field min-w-[12rem] flex-1"
                placeholder="Replicate token (r8_…)"
                aria-label="Replicate API token"
                autoComplete="off"
                value={repToken}
                onChange={(e) => setRepToken(e.target.value)}
              />
              <input
                type="text"
                className="field min-w-[12rem] flex-1"
                placeholder="model (owner/name) — optional"
                aria-label="Replicate image model"
                value={repModel}
                onChange={(e) => setRepModel(e.target.value)}
              />
              <button onClick={saveReplicate} className="btn-ember">
                Save token
              </button>
            </div>
            <label className="mt-4 flex items-center gap-2 text-sm text-bone">
              <input
                type="checkbox"
                className="h-4 w-4 accent-ember"
                checked={!!secrets.data?.asset_gen}
                onChange={(e) => saveAssetGen(e.target.checked)}
              />
              <span>
                Generate real images <span className="font-mono text-ash">(asset_gen)</span>
                {secrets.data?.asset_gen ? (
                  <span className="text-plasma"> — ON</span>
                ) : (
                  <span className="text-ash"> — off</span>
                )}
                {!secrets.data?.replicate ? (
                  <span className="text-ember"> · needs a token above to take effect</span>
                ) : null}
              </span>
            </label>
            {repMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{repMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="visual">
          <PanelHead
            label="Visual self-heal"
            right={
              <Pill tone={data?.visual_self_heal ? "plasma" : "ash"}>
                {data?.visual_self_heal ? "ON" : "off"}
              </Pill>
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              When enabled, UI web builds are served in a browser, screenshotted,
              judged against the original brief, and repaired before liveness. It
              soft-skips when Playwright or a vision provider is unavailable.
            </p>
            <label className="flex items-center gap-2 text-sm text-bone">
              <input
                type="checkbox"
                className="h-4 w-4 accent-ember"
                checked={!!data?.visual_self_heal}
                onChange={(e) => saveVisualSelfHeal(e.target.checked)}
              />
              <span>
                Drive rendered UI{" "}
                <span className="font-mono text-ash">(visual_self_heal)</span>
                {data?.visual_self_heal ? (
                  <span className="text-plasma"> — ON</span>
                ) : (
                  <span className="text-ash"> — off</span>
                )}
              </span>
            </label>
            {visualMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{visualMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="improve">
          <PanelHead
            label="Agentic improve"
            right={
              <Pill tone={data?.improve_agentic ? "plasma" : "ash"}>
                {data?.improve_agentic ? "ON" : "off"}
              </Pill>
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              When enabled, an Improve goal runs a whole-project agentic session
              that can create new pages and touch multiple files (like builds
              do). Broken rewrites are auto-reverted, and the classic
              single-file improver remains the automatic fallback.
            </p>
            <label className="flex items-center gap-2 text-sm text-bone">
              <input
                type="checkbox"
                className="h-4 w-4 accent-ember"
                checked={!!data?.improve_agentic}
                onChange={(e) => saveImproveAgentic(e.target.checked)}
              />
              <span>
                Multi-file Improve{" "}
                <span className="font-mono text-ash">(improve_agentic)</span>
                {data?.improve_agentic ? (
                  <span className="text-plasma"> — ON</span>
                ) : (
                  <span className="text-ash"> — off</span>
                )}
              </span>
            </label>
            {agenticMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{agenticMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="gates">
          <PanelHead
            label="Verification gates"
            right={
              gates.error ? (
                <Pill tone="ember">unreachable</Pill>
              ) : (
                <Pill tone="plasma">
                  {(gates.data?.gates || []).filter((g) => g.enabled).length}/
                  {(gates.data?.gates || []).length} on
                </Pill>
              )
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              End-of-build gates that verify a delivery before it ships —
              headless sim, playtest, liveness, SEO, and the MCP / RAG /
              workflow / CLI contract and scripted terminal checks. Advisory gates feed the repair
              loop; disabling one skips that verification for future builds.
            </p>
            <div className="grid gap-2">
              {(gates.data?.gates || []).map((g) => (
                <label
                  key={g.gate}
                  className="flex items-center gap-2 text-sm text-bone"
                >
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-ember"
                    checked={!!g.enabled}
                    onChange={(e) => saveGate(g.gate, e.target.checked)}
                  />
                  <span>
                    <span className="font-mono">{g.gate}</span>{" "}
                    <span className="text-ash">
                      ({(g.stacks || []).join(", ")})
                    </span>
                    {g.via_headless_gate ? (
                      <span className="text-ash"> · rides the headless gate</span>
                    ) : null}
                  </span>
                </label>
              ))}
            </div>
            {gateMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{gateMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="messaging">
          <PanelHead
            label="Messaging channels"
            right={
              integrations.error ? (
                <Pill tone="ember">unreachable</Pill>
              ) : (
                <Pill tone={integrations.data?.listener?.running ? "plasma" : "ash"}>
                  bot · {integrations.data?.listener?.running ? "listening" : "stopped"}
                </Pill>
              )
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              Wire the swarm into chat. Save a bot token (+ chat id / channel),
              then start the bot to submit briefs from Telegram (<span className="font-mono text-bone">/build &lt;idea&gt;</span>) and
              receive build notifications.
            </p>
            <div className="mb-4 flex flex-wrap gap-2">
              <button onClick={() => controlListener("start")} className="btn-ember">
                Start bot
              </button>
              <button onClick={() => controlListener("stop")} className="btn-ghost">
                Stop
              </button>
              <button onClick={() => controlListener("test")} className="btn-ghost">
                Send test
              </button>
            </div>
            <div className="mb-4 flex flex-wrap gap-2">
              {CHANNELS.map((c) => {
                const info = chData[c] || {};
                const ok = info.configured;
                return (
                  <Pill key={c} tone={ok ? "plasma" : "ash"}>
                    {c}
                    {ok ? " ✓" : " ·"}
                    {ok && info.target_set ? (
                      <span className="ml-1 text-plasma-soft">+ target</span>
                    ) : null}
                  </Pill>
                );
              })}
            </div>
            <div className="flex flex-wrap gap-2">
              <select
                aria-label="Messaging channel"
                value={channel}
                onChange={(e) => setChannel(e.target.value)}
                className="field max-w-[10rem]"
              >
                {CHANNELS.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
              <input
                type="password"
                className="field min-w-[12rem] flex-1"
                placeholder={`${channel} token`}
                aria-label={channel + " token"}
                autoComplete="off"
                value={chToken}
                onChange={(e) => setChToken(e.target.value)}
              />
              <input
                type="text"
                className="field min-w-[10rem] flex-1"
                placeholder="target (chat id / channel) — optional"
                aria-label="Messaging target"
                value={chTarget}
                onChange={(e) => setChTarget(e.target.value)}
              />
              <button onClick={saveChannel} className="btn-ember">
                Save channel
              </button>
            </div>
            {chMsg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{chMsg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel id="auth">
          <PanelHead label="API auth token" />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              Stored locally in your browser and sent as a Bearer token.
            </p>
            <div className="flex flex-wrap gap-2">
              <input
                type="password"
                className="field min-w-[12rem] flex-1"
                placeholder="auth token"
                aria-label="Control-plane auth token"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
              <button onClick={saveToken} className="btn-ember">
                {saved ? "Saved" : "Save"}
              </button>
            </div>
          </div>
        </Panel>

        <Panel id="runtime">
          <PanelHead
            label="Runtime"
            right={<span className="font-mono text-[11px] text-ash">read-only</span>}
          />
          {error ? (
            <div role="alert" className="px-4 py-3 text-sm text-ember">{String(error.message)}</div>
          ) : flags ? (
            <div>
              {Object.entries(flags).map(([k, v]) => (
                <Row key={k} label={k} value={v} />
              ))}
            </div>
          ) : (
            <Empty icon="≋">Loading runtime flags…</Empty>
          )}
        </Panel>
      </div>
    </div>
  );
}
