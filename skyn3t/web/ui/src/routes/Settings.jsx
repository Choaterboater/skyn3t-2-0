import React, { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { queryFn, apiPost } from "../api.js";
import { PageHeader, Panel, PanelHead, Pill, Empty } from "../components/ui.jsx";

function Row({ label, value }) {
  return (
    <div className="flex items-center justify-between border-b border-hairline/60 px-4 py-2.5 last:border-b-0">
      <span className="font-mono text-[11px] uppercase tracking-eyebrow text-ash">{label}</span>
      <span className="font-mono text-xs text-bone">{String(value)}</span>
    </div>
  );
}

const BACKENDS = ["auto", "stub", "claude_cli", "kimi_cli", "copilot_cli", "openrouter"];
const PROVIDERS = ["openrouter", "anthropic", "openai", "kimi"];
const CHANNELS = ["telegram", "discord", "slack"];
const APP_TYPES = ["auto", "product_app", "dashboard", "landing_page", "crud_app", "saas_product", "game", "api_service", "developer_tool", "data_viz", "mobile_app", "desktop_app"];
const ENGINES = ["auto", "dom", "browser_native", "phaser", "godot", "bevy", "raylib", "expo", "tauri", "server", "python", "none"];

export default function Settings() {
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
  const [msg, setMsg] = useState("");

  const [ghToken, setGhToken] = useState("");
  const [ghMsg, setGhMsg] = useState("");

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
  const [model, setModel] = useState("");
  const [modelMsg, setModelMsg] = useState("");
  useEffect(() => {
    const cur = secrets.data?.preferred_model;
    if (cur !== undefined) setModel(cur || "");
  }, [secrets.data?.preferred_model]);
  async function saveModel(m) {
    setModel(m);
    try {
      await apiPost("/settings/model", { model: m });
      setModelMsg(m ? `pinned → ${m}` : "auto — smart routing");
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

  function saveToken() {
    if (typeof localStorage !== "undefined") {
      if (token) localStorage.setItem("skyn3t_token", token);
      else localStorage.removeItem("skyn3t_token");
    }
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  }

  async function saveKey() {
    try {
      const r = await apiPost("/llm/key", { provider, key });
      setKey("");
      setMsg(`${provider}: ${r.configured ? "saved" : "cleared"} → backend ${r.backend}`);
      secrets.refetch();
    } catch (e) {
      setMsg(String(e.message));
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
      setMsg(`backend → ${r.active}`);
      secrets.refetch();
    } catch (e) {
      setMsg(String(e.message));
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
      }
    : null;

  const active = secrets.data?.backend;
  const chData = integrations.data?.channels || {};

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
        <Panel>
          <PanelHead
            label="LLM backend"
            right={
              <Pill tone={active ? "plasma" : "ash"}>active · {active || "…"}</Pill>
            }
          />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              <span className="font-mono text-bone">auto</span> uses OpenRouter if a
              key is set, else a local CLI (claude/kimi/copilot), else the offline
              stub. Pick one to pin it.
            </p>
            <div className="flex flex-wrap gap-2">
              {BACKENDS.map((b) => {
                const sel = secrets.data?.backend_pref === b;
                return (
                  <button
                    key={b}
                    onClick={() => pickBackend(b)}
                    className={`badge font-mono transition-colors ${
                      sel
                        ? "border-ember/60 bg-ember/10 text-ember"
                        : "border-hairline text-ash hover:border-ember/40 hover:text-bone"
                    }`}
                  >
                    {b}
                  </button>
                );
              })}
            </div>
            {msg ? (
              <p className="mt-3 font-mono text-[11px] text-plasma">{msg}</p>
            ) : null}
          </div>
        </Panel>

        <Panel>
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

        <Panel>
          <PanelHead label="API key" />
          <div className="p-4">
            <p className="mb-4 text-sm text-ash">
              Stored in the server&apos;s <code className="font-mono text-bone">.env</code>.
              Setting an OpenRouter key switches{" "}
              <span className="font-mono text-bone">auto</span> to real cloud
              generation immediately.
            </p>
            <div className="flex flex-wrap gap-2">
              <select
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
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
              <button onClick={saveKey} className="btn-ember">
                Save key
              </button>
            </div>
            <div className="mt-4 border-t border-hairline pt-4">
              <p className="mb-2 text-sm text-ash">
                Model — which OpenRouter model skyn3t builds with.{" "}
                <span className="font-mono text-bone">auto</span> lets the learned
                router pick per task; or pin one from the live list (it updates with
                the newest models automatically).
              </p>
              <div className="flex flex-wrap items-center gap-2">
                <select
                  value={model}
                  onChange={(e) => saveModel(e.target.value)}
                  className="field min-w-[16rem] flex-1"
                >
                  <option value="">auto — smart routing</option>
                  {(models.data?.models || []).map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
                <span className="font-mono text-[11px] text-ash/60">
                  {models.isLoading
                    ? "loading…"
                    : models.data?.note
                      ? models.data.note
                      : `${(models.data?.models || []).length} models`}
                </span>
              </div>
              {modelMsg ? (
                <p className="mt-2 font-mono text-[11px] text-plasma">{modelMsg}</p>
              ) : null}
            </div>
          </div>
        </Panel>

        <Panel>
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

        <Panel>
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
                value={repToken}
                onChange={(e) => setRepToken(e.target.value)}
              />
              <input
                type="text"
                className="field min-w-[12rem] flex-1"
                placeholder="model (owner/name) — optional"
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

        <Panel>
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

        <Panel>
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

        <Panel>
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
              workflow / CLI contract checks. Advisory gates feed the repair
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

        <Panel>
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
                value={chToken}
                onChange={(e) => setChToken(e.target.value)}
              />
              <input
                type="text"
                className="field min-w-[10rem] flex-1"
                placeholder="target (chat id / channel) — optional"
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

        <Panel>
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
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
              <button onClick={saveToken} className="btn-ember">
                {saved ? "Saved" : "Save"}
              </button>
            </div>
          </div>
        </Panel>

        <Panel>
          <PanelHead
            label="Runtime"
            right={<span className="font-mono text-[11px] text-ash">read-only</span>}
          />
          {error ? (
            <div className="px-4 py-3 text-sm text-ember">{String(error.message)}</div>
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
