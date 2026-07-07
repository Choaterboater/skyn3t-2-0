import React, { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiFetch, apiPost } from "../api.js";
import {
  PageHeader,
  Panel,
  PanelHead,
  Pill,
  Empty,
  SignalGrid,
  verdictTone,
} from "../components/ui.jsx";

function fmtMB(bytes) {
  if (bytes == null) return "—";
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

function fmtCost(usd) {
  return usd == null ? "—" : `$${Number(usd).toFixed(4)}`;
}

function aiEvidence(project) {
  const skills = Array.isArray(project.skills_used) ? project.skills_used : [];
  const recall = Array.isArray(project.recall_used) ? project.recall_used : [];
  const stageSkills =
    project.stage_skills_used && typeof project.stage_skills_used === "object"
      ? project.stage_skills_used
      : {};
  const roleStages = Object.values(stageSkills).filter(
    (items) => Array.isArray(items) && items.length > 0,
  ).length;
  const promptCount = Number(project.prompt_count || 0);
  const title = [
    skills.length ? `skills: ${skills.join(", ")}` : "skills: 0",
    `recall: ${recall.length}`,
    `stage roles: ${roleStages}`,
    `prompts: ${promptCount}`,
  ].join(" · ");
  return { skills, recall, roleStages, promptCount, title };
}

// created_at/updated_at may be epoch seconds, epoch ms, a numeric string, or an
// ISO string — normalize to a comparable ms timestamp (0 when absent/unparseable).
function toTime(v) {
  if (v == null || v === "") return 0;
  if (typeof v === "number") return v < 1e12 ? v * 1000 : v;
  const n = Number(v);
  if (!Number.isNaN(n) && String(v).trim() !== "") return n < 1e12 ? n * 1000 : n;
  const t = Date.parse(v);
  return Number.isNaN(t) ? 0 : t;
}

function fmtDate(v) {
  const t = toTime(v);
  if (!t) return "—";
  return new Date(t).toLocaleString(undefined, {
    year: "2-digit", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

const _NUMERIC_KEYS = ["score", "cost_usd", "size_bytes"];
const _DATE_KEYS = ["created_at", "updated_at"];

// A clickable column header that drives table sort. Text columns default to
// ascending; numeric/date columns default to descending (newest/highest first).
function SortHeader({ label, sortKey: key, sort, setSort, align }) {
  const active = sort.key === key;
  const arrow = !active ? "↕" : sort.dir === "asc" ? "↑" : "↓";
  const defaultDir = _NUMERIC_KEYS.includes(key) || _DATE_KEYS.includes(key) ? "desc" : "asc";
  return (
    <th
      onClick={() =>
        setSort((s) =>
          s.key === key
            ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
            : { key, dir: defaultDir },
        )
      }
      className={`cursor-pointer select-none px-4 py-2 font-normal hover:text-bone ${
        align === "right" ? "text-right" : ""
      }`}
      title="Click to sort"
    >
      {label}
      <span className={`ml-1 ${active ? "text-ember" : "text-ash/40"}`}>{arrow}</span>
    </th>
  );
}

// ---------------------------------------------------------------------------
// Live serve-status. Seed from GET /api/studio/serve, then replay the shared
// serve.started/serve.stopped event stream on top so the table reflects starts
// and stops in real time. The 15s query refetch self-heals if an old start
// event rolls off the capped (200) event buffer.
// ---------------------------------------------------------------------------
function useServedMap(stream, seed) {
  return useMemo(() => {
    const map = {};
    for (const a of seed?.running || []) {
      if (a.slug) map[a.slug] = a;
    }
    for (const e of stream?.events || []) {
      const slug = e.payload?.slug;
      if (!slug) continue;
      if (e.type === "serve.started") {
        map[slug] = {
          slug,
          url: e.payload.url,
          port: e.payload.port,
          status: "running",
        };
      } else if (e.type === "serve.stopped") {
        delete map[slug];
      }
    }
    return map;
  }, [stream?.events, seed]);
}

// Latest improve.* event for a slug (or a correlation id we dispatched), mapped
// to a compact status line. ImproveEngine emits by manifest slug, so we also
// match the correlation id returned at dispatch.
function improveStatusFor(stream, slug, cid) {
  let latest = null;
  for (const e of stream?.events || []) {
    if (!e.type?.startsWith("improve.")) continue;
    if (e.payload?.slug === slug || (cid && e.correlation_id === cid)) latest = e;
  }
  if (!latest) return null;
  const p = latest.payload || {};
  switch (latest.type) {
    case "improve.started":
      return { tone: "text-ember", text: "improving…" };
    case "improve.stage":
      return { tone: "text-ash", text: `${p.stage || "working"}…` };
    case "improve.completed": {
      // The backend is honest about no-ops (files_changed + no_targets_found /
      // no_files_changed); surface it so "improved" can't mean "changed nothing".
      const n = (p.files_changed || []).length;
      if (n === 0) {
        const why = p.detail?.no_targets_found
          ? "no matching files found"
          : "no edits applied";
        return { tone: "text-ember", text: `no changes — ${why}` };
      }
      const files = `${n} file${n === 1 ? "" : "s"}`;
      return p.proof_passed
        ? { tone: "text-plasma", text: `improved · ${files} · score ${p.score ?? "—"}` }
        : { tone: "text-ember", text: `${files} changed · no_go` };
    }
    case "improve.failed":
      return { tone: "text-ember", text: `failed: ${p.error || "unknown"}` };
    default:
      return null;
  }
}

function CleanupPanel({ qc }) {
  const [scan, setScan] = useState(null);
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState(null);

  const apply = useMutation({
    mutationFn: () => apiPost("/projects/cleanup", { dry_run: false }),
    onSuccess: () => {
      setScan(null);
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  async function doScan() {
    setScanning(true);
    setScanError(null);
    try {
      const result = await apiFetch("/projects/cleanup");
      setScan(result);
    } catch (err) {
      setScanError(String(err.message));
    } finally {
      setScanning(false);
    }
  }

  const CATEGORIES = [
    { key: "failed", label: "Failed builds" },
    { key: "superseded", label: "Superseded" },
    { key: "orphaned_worktrees", label: "Orphaned worktrees" },
    { key: "orphaned_projects", label: "Orphaned projects" },
    { key: "stray_previews", label: "Stray previews" },
  ];

  const totalBytes = scan
    ? CATEGORIES.reduce((acc, { key }) => {
        const items = scan[key] || [];
        return acc + items.reduce((s, i) => s + (i.size_bytes || 0), 0);
      }, 0)
    : 0;

  return (
    <Panel className="mb-6 overflow-hidden">
      <PanelHead
        label="Cleanup recommendations"
        right={
          <div className="flex items-center gap-2">
            {scan ? (
              <span className="font-mono text-[11px] text-ash">
                would free{" "}
                <span className="text-ember">{fmtMB(totalBytes)}</span>
              </span>
            ) : null}
            {apply.data ? (
              <span className="font-mono text-[11px] text-plasma">
                freed {fmtMB(apply.data.freed_bytes)}
              </span>
            ) : null}
            <button
              onClick={doScan}
              disabled={scanning}
              className="btn-ghost disabled:opacity-50"
            >
              {scanning ? "Scanning…" : "Scan"}
            </button>
            {scan ? (
              <button
                onClick={() => apply.mutate()}
                disabled={apply.isPending}
                className="btn-ember disabled:opacity-50"
              >
                {apply.isPending ? "Applying…" : "Apply"}
              </button>
            ) : null}
          </div>
        }
      />
      {scanError ? (
        <p className="px-4 py-3 font-mono text-xs text-ember">{scanError}</p>
      ) : null}
      {apply.isError ? (
        <p className="px-4 py-3 font-mono text-xs text-ember">
          {String(apply.error.message)}
        </p>
      ) : null}
      {scan ? (
        <div className="divide-y divide-hairline/60">
          {CATEGORIES.map(({ key, label }) => {
            const items = scan[key] || [];
            const bytes = items.reduce((s, i) => s + (i.size_bytes || 0), 0);
            return (
              <div key={key} className="px-4 py-3">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-xs text-bone">{label}</span>
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-[11px] text-ash">
                      {items.length} item{items.length !== 1 ? "s" : ""}
                    </span>
                    <span className="font-mono text-[11px] text-ember">
                      {fmtMB(bytes)}
                    </span>
                  </div>
                </div>
                {items.length > 0 ? (
                  <ul className="mt-1.5 space-y-0.5">
                    {items.map((item, idx) => (
                      <li
                        key={`${item.path}-${idx}`}
                        className="font-mono text-[11px] text-ash/70"
                      >
                        {item.path}{" "}
                        {item.reason ? (
                          <span className="text-ash/50">· {item.reason}</span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : (
        <div className="px-4 py-3 font-mono text-[11px] text-ash">
          Run a scan to see failed builds, superseded outputs, orphaned worktrees,
          and stray previews that are safe cleanup candidates.
        </div>
      )}
    </Panel>
  );
}

// Inline "improve this project toward a goal" form, shown as an expanded sub-row.
// One-shot dispatch — the iterative side-by-side mode lives in the Workspace.
function ImproveInline({ slug, stream }) {
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [cid, setCid] = useState(null);

  async function send() {
    if (!goal.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await apiPost("/studio/improve", { slug, goal });
      if (r.accepted) {
        setCid(r.correlation_id || null);
        setGoal("");
      } else {
        setErr(r.reason || "improve unavailable");
      }
    } catch (e) {
      setErr(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  const status = improveStatusFor(stream, slug, cid);

  return (
    <div className="bg-ink/30 px-4 py-3">
      <div className="flex items-start gap-2">
        <textarea
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          placeholder="Describe what to add or change, in plain English…"
          rows={2}
          className="flex-1 resize-none rounded border border-hairline bg-ink/60 px-3 py-2 font-mono text-xs text-bone placeholder:text-ash/50 focus:border-ember focus:outline-none"
        />
        <button
          onClick={send}
          disabled={busy || !goal.trim()}
          className="btn-ember disabled:opacity-50"
        >
          {busy ? "Sending…" : "Improve"}
        </button>
      </div>
      <div className="mt-1.5 flex items-center justify-between">
        {err ? (
          <span className="font-mono text-[11px] text-ember">{err}</span>
        ) : cid ? (
          <span className="font-mono text-[11px] text-ash/60">
            dispatched · {cid.slice(0, 8)}
            {status ? (
              <span className={`ml-2 ${status.tone}`}>{status.text}</span>
            ) : null}
          </span>
        ) : (
          <span className="font-mono text-[11px] text-ash/60">
            runs audit → edit → verify → deliver · re-serve to see the change
          </span>
        )}
      </div>
    </div>
  );
}

// Inline "the exact prompt(s) this build sent the model", shown as an expanded
// sub-row. Loaded lazily (prompts run 10-50 KB each) from the manifest via
// GET /projects/{slug}/prompts. Answers "what did skyn3t actually ask the model?"
function PromptsInline({ slug }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["project-prompts", slug],
    queryFn: queryFn(`/projects/${slug}/prompts`),
  });
  const prompts = data?.prompts || [];
  return (
    <div className="bg-ink/30 px-4 py-3">
      {isLoading ? (
        <span className="font-mono text-[11px] text-ash/60">loading prompts…</span>
      ) : error ? (
        <span className="font-mono text-[11px] text-ember">
          {String(error.message || error)}
        </span>
      ) : prompts.length === 0 ? (
        <span className="font-mono text-[11px] text-ash/60">
          No prompts recorded (older build, or the offline scaffold path).
        </span>
      ) : (
        <div className="flex flex-col gap-2">
          <span className="font-mono text-[11px] text-ash/60">
            The exact prompt{prompts.length === 1 ? "" : "s"} this build sent the
            model — brief + injected directives + recalled knowledge.
          </span>
          {prompts.map((pr, i) => (
            <details key={i} className="rounded border border-hairline bg-ink/60">
              <summary className="cursor-pointer px-3 py-2 font-mono text-xs text-bone">
                {pr.stage || `prompt ${i + 1}`}
                <span className="text-ash/60">
                  {" "}
                  · {(pr.chars ?? (pr.text || "").length).toLocaleString()} chars
                </span>
              </summary>
              <pre className="max-h-96 overflow-auto whitespace-pre-wrap border-t border-hairline px-3 py-2 font-mono text-[11px] text-ash">
                {pr.text || ""}
              </pre>
            </details>
          ))}
        </div>
      )}
    </div>
  );
}

function ServeCell({ slug, served, busy, err, onServe, onStop }) {
  const running = !!served && (served.status === "running" || !!served.url);
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-2">
        {running ? (
          <>
            <span className="h-1.5 w-1.5 rounded-full bg-plasma" />
            <a
              href={served.url}
              target="_blank"
              rel="noopener noreferrer"
              className="font-mono text-[11px] text-plasma underline hover:text-plasma/70"
              title={served.url}
            >
              :{served.port} ↗
            </a>
            <button
              onClick={() => onStop(slug)}
              disabled={busy === "stopping"}
              className="btn-ghost disabled:opacity-50"
            >
              {busy === "stopping" ? "…" : "Stop"}
            </button>
          </>
        ) : (
          <button
            onClick={() => onServe(slug)}
            disabled={!!busy}
            className="btn-ghost text-plasma/80 hover:text-plasma disabled:opacity-50"
          >
            {busy === "serving" ? "Starting…" : "Serve"}
          </button>
        )}
      </div>
      {err ? (
        <span
          className="max-w-[180px] truncate font-mono text-[10px] text-ember"
          title={err}
        >
          {err}
        </span>
      ) : null}
    </div>
  );
}

function ShipCell({ project }) {
  const qc = useQueryClient();
  const [plan, setPlan] = useState(null);
  const [err, setErr] = useState(null);
  const slug = project.slug;
  const deployments = Array.isArray(project.deployments) ? project.deployments : [];
  const latest = deployments.length ? deployments[deployments.length - 1] : null;
  const liveUrl = project.live_url || latest?.url || "";
  const manifestPlan = project.deploy_plan?.deployable ? project.deploy_plan : null;
  const visiblePlan = plan?.plan || manifestPlan;
  const defaultTarget = visiblePlan?.targets?.[0] || "";

  const deploy = useMutation({
    mutationFn: () => apiPost("/studio/deploy", { slug, target: defaultTarget }),
    onSuccess: (result) => {
      setPlan(result);
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
    onError: (e) => setErr(String(e.message || e)),
  });
  const deployCheck = deploy.data?.deploy_check || project.deploy_check || null;
  const deployCheckLabel = deployCheck?.ok
    ? "verified"
    : deployCheck?.skipped
      ? "deploy check skipped"
      : Array.isArray(deployCheck?.issues) && deployCheck.issues.length
        ? "deploy issues"
        : "";

  async function loadPlan() {
    setErr(null);
    try {
      const result = await apiFetch(`/studio/deploy/plan?slug=${encodeURIComponent(slug)}`);
      setPlan(result);
    } catch (e) {
      setErr(String(e.message || e));
    }
  }

  return (
    <div className="flex max-w-[240px] flex-col gap-1">
      {liveUrl ? (
        <a
          href={liveUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="truncate font-mono text-[11px] text-plasma underline hover:text-plasma/70"
          title={liveUrl}
        >
          live ↗
        </a>
      ) : null}
      <div className="flex items-center gap-2">
        <button
          onClick={loadPlan}
          className="btn-ghost text-plasma/80 hover:text-plasma"
          title="View deploy plan"
        >
          Plan
        </button>
        {visiblePlan?.serves_url ? (
          <button
            onClick={() => deploy.mutate()}
            disabled={deploy.isPending || !defaultTarget}
            className="btn-ghost text-ember/80 hover:text-ember disabled:opacity-50"
            title="Deploy using the first available target"
          >
            {deploy.isPending ? "…" : "Ship"}
          </button>
        ) : null}
      </div>
      {visiblePlan ? (
        <div className="font-mono text-[10px] leading-snug text-ash/70">
          <div className="truncate" title={visiblePlan.command || visiblePlan.notes}>
            {visiblePlan.kind || "deploy"} · {defaultTarget || "no target"}
          </div>
          {visiblePlan.command ? (
            <div className="truncate text-ash/50" title={visiblePlan.command}>
              {visiblePlan.command}
            </div>
          ) : null}
        </div>
      ) : null}
      {deployCheckLabel ? (
        <span
          className={
            deployCheck?.ok
              ? "font-mono text-[10px] text-mint"
              : "font-mono text-[10px] text-ember"
          }
          title={deployCheck?.reason || deployCheckLabel}
        >
          {deployCheckLabel}
        </span>
      ) : null}
      {deploy.data?.result?.error ? (
        <span className="truncate font-mono text-[10px] text-ember" title={deploy.data.result.error}>
          {deploy.data.result.error}
        </span>
      ) : err ? (
        <span className="truncate font-mono text-[10px] text-ember" title={err}>
          {err}
        </span>
      ) : null}
    </div>
  );
}

export default function Projects({ stream }) {
  const qc = useQueryClient();
  const [confirmSlug, setConfirmSlug] = useState(null);
  const [improveSlug, setImproveSlug] = useState(null);
  const [promptsSlug, setPromptsSlug] = useState(null);
  const [busy, setBusy] = useState({}); // slug -> "serving" | "stopping"
  const [serveErr, setServeErr] = useState({}); // slug -> message
  const [sort, setSort] = useState({ key: "updated_at", dir: "desc" });

  const { data, isLoading, error } = useQuery({
    queryKey: ["projects"],
    queryFn: queryFn("/projects"),
  });

  const serveQuery = useQuery({
    queryKey: ["serve-status"],
    queryFn: queryFn("/studio/serve"),
    refetchInterval: 15000,
  });
  const served = useServedMap(stream, serveQuery.data);

  const del = useMutation({
    mutationFn: (slug) => apiFetch(`/projects/${slug}`, { method: "DELETE" }),
    onSuccess: () => {
      setConfirmSlug(null);
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  async function serve(slug) {
    setBusy((b) => ({ ...b, [slug]: "serving" }));
    setServeErr((e) => ({ ...e, [slug]: null }));
    try {
      const r = await apiPost("/studio/serve", { slug });
      if (r.status !== "running") {
        const reason =
          r.detail?.install_error?.error ||
          r.detail?.reason ||
          r.detail?.log_tail ||
          `not servable (${r.status})`;
        setServeErr((e) => ({ ...e, [slug]: reason }));
      }
      qc.invalidateQueries({ queryKey: ["serve-status"] });
    } catch (e) {
      setServeErr((er) => ({ ...er, [slug]: String(e.message) }));
    } finally {
      setBusy((b) => ({ ...b, [slug]: null }));
    }
  }

  async function stopServe(slug) {
    setBusy((b) => ({ ...b, [slug]: "stopping" }));
    try {
      await apiPost("/studio/serve/stop", { slug });
      qc.invalidateQueries({ queryKey: ["serve-status"] });
    } catch (e) {
      setServeErr((er) => ({ ...er, [slug]: String(e.message) }));
    } finally {
      setBusy((b) => ({ ...b, [slug]: null }));
    }
  }

  const projects = Array.isArray(data) ? data : data?.projects || [];
  const liveCount = Object.keys(served).length;
  const shippableCount = projects.filter((project) => {
    const state = String(project.verdict || project.status || "").toLowerCase();
    return state === "go" || state === "completed" || state === "applied";
  }).length;
  const wastedSpend = projects.reduce(
    (sum, project) => sum + Number(project.wasted_usd || 0),
    0,
  );
  const projectSignals = [
    { label: "projects", value: String(projects.length) },
    { label: "live", value: String(liveCount) },
    { label: "shippable", value: String(shippableCount) },
    { label: "wasted", value: fmtCost(wastedSpend) },
  ];

  const sorted = useMemo(() => {
    const arr = [...projects];
    const dir = sort.dir === "asc" ? 1 : -1;
    arr.sort((a, b) => {
      let av, bv;
      if (_NUMERIC_KEYS.includes(sort.key)) {
        av = Number(a[sort.key]) || 0;
        bv = Number(b[sort.key]) || 0;
      } else if (_DATE_KEYS.includes(sort.key)) {
        av = toTime(a[sort.key]);
        bv = toTime(b[sort.key]);
      } else {
        av = String(a[sort.key] ?? "").toLowerCase();
        bv = String(b[sort.key] ?? "").toLowerCase();
      }
      return av < bv ? -dir : av > bv ? dir : 0;
    });
    return arr;
  }, [projects, sort]);

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Project Vault"
        title="Projects"
        sub="Run, refine, preview, and clean up everything the foundry has built."
        actions={
          <div className="flex items-center gap-2">
            {liveCount > 0 ? (
              <span className="badge border-plasma/40 text-plasma">
                {liveCount} live
              </span>
            ) : null}
            <span className="badge border-hairline text-ash">
              {projects.length} project{projects.length !== 1 ? "s" : ""}
            </span>
          </div>
        }
      />

      {error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Could not load projects: {String(error.message)}
        </Panel>
      ) : null}

      <Panel className="mb-4 p-3">
        <SignalGrid label="Projects cockpit" items={projectSignals} />
      </Panel>

      <CleanupPanel qc={qc} />

      <Panel className="overflow-hidden">
        <PanelHead
          label="Project list"
          right={
            <span className="font-mono text-[11px] text-ash">
              {projects.length} total
            </span>
          }
        />
        {isLoading ? (
          <Empty icon="≋">Loading projects…</Empty>
        ) : projects.length === 0 ? (
          <Empty icon="▤">No projects yet. Forge a brief in Studio to get started.</Empty>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead>
                <tr className="eyebrow border-b border-hairline text-ash">
                  <SortHeader label="Slug" sortKey="slug" sort={sort} setSort={setSort} />
                  <SortHeader label="Stack" sortKey="stack" sort={sort} setSort={setSort} />
                  <SortHeader label="Status" sortKey="status" sort={sort} setSort={setSort} />
                  <SortHeader label="Score" sortKey="score" sort={sort} setSort={setSort} />
                  <th className="px-4 py-2 font-normal">AI</th>
                  <SortHeader label="Cost" sortKey="cost_usd" sort={sort} setSort={setSort} />
                  <SortHeader label="Size" sortKey="size_bytes" sort={sort} setSort={setSort} />
                  <SortHeader label="Updated" sortKey="updated_at" sort={sort} setSort={setSort} />
                  <th className="px-4 py-2 font-normal">Serve</th>
                  <th className="px-4 py-2 font-normal">Ship</th>
                  <th className="px-4 py-2 font-normal">Preview</th>
                  <th className="px-4 py-2 font-normal"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline/60">
                {sorted.map((p) => {
                  const isConfirming = confirmSlug === p.slug;
                  const isImproving = improveSlug === p.slug;
                  const isShowingPrompts = promptsSlug === p.slug;
                  const ai = aiEvidence(p);
                  return (
                    <React.Fragment key={p.slug}>
                      <tr>
                        <td className="px-4 py-2 font-mono text-xs text-bone">
                          {p.slug}
                        </td>
                        <td className="px-4 py-2 font-mono text-xs text-ash">
                          {p.stack || "—"}
                        </td>
                        <td className="px-4 py-2">
                          <Pill tone={verdictTone(p.status || p.verdict)}>
                            {p.status || p.verdict || "—"}
                          </Pill>
                        </td>
                        <td className="px-4 py-2 font-mono text-xs text-ash">
                          {p.score ?? "—"}
                        </td>
                        <td className="px-4 py-2 font-mono text-[11px] text-ash">
                          <div className="whitespace-nowrap" title={ai.title}>
                            <span className={ai.skills.length ? "text-plasma" : "text-ash/50"}>
                              skills {ai.skills.length}
                            </span>
                            <span className="mx-1 text-ash/40">·</span>
                            <span className={ai.recall.length ? "text-bone" : "text-ash/50"}>
                              recall {ai.recall.length}
                            </span>
                          </div>
                          <div className="whitespace-nowrap text-ash/60" title={ai.title}>
                            roles {ai.roleStages} · prompts {ai.promptCount}
                          </div>
                        </td>
                        <td className="px-4 py-2 font-mono text-xs">
                          <span className={p.wasted_usd ? "text-ember" : "text-ash"}>
                            {fmtCost(p.cost_usd)}
                          </span>
                          {p.wasted_usd ? (
                            <span
                              className="ml-1 text-[10px] text-ember/70"
                              title="no_go build — this spend produced nothing shippable"
                            >
                              wasted
                            </span>
                          ) : null}
                        </td>
                        <td className="px-4 py-2 font-mono text-xs text-ash">
                          {fmtMB(p.size_bytes)}
                        </td>
                        <td
                          className="whitespace-nowrap px-4 py-2 font-mono text-xs text-ash"
                          title={p.updated_at ? String(p.updated_at) : ""}
                        >
                          {fmtDate(p.updated_at || p.created_at)}
                        </td>
                        <td className="px-4 py-2">
                          <ServeCell
                            slug={p.slug}
                            served={served[p.slug]}
                            busy={busy[p.slug]}
                            err={serveErr[p.slug]}
                            onServe={serve}
                            onStop={stopServe}
                          />
                        </td>
                        <td className="px-4 py-2">
                          <ShipCell project={p} />
                        </td>
                        <td className="px-4 py-2">
                          <div className="flex items-center gap-3">
                            {p.has_preview ? (
                              <a
                                href={`/api/projects/${p.slug}/index.html`}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="font-mono text-[11px] text-plasma hover:text-plasma/70 underline"
                              >
                                preview ↗
                              </a>
                            ) : (
                              <span className="font-mono text-[11px] text-ash/40">—</span>
                            )}
                            <Link
                              to={`/workspace?slug=${encodeURIComponent(p.slug)}`}
                              className="font-mono text-[11px] text-ember hover:text-ember/70 underline"
                            >
                              workspace ↗
                            </Link>
                          </div>
                        </td>
                        <td className="px-4 py-2 text-right">
                          {isConfirming ? (
                            <div className="flex flex-col items-end gap-1">
                              <div className="flex items-center justify-end gap-2">
                                <span className="font-mono text-[11px] text-ember">
                                  Trash it?
                                </span>
                                <button
                                  onClick={() => del.mutate(p.slug)}
                                  disabled={del.isPending}
                                  className="btn-ember disabled:opacity-50"
                                >
                                  {del.isPending ? "…" : "Yes"}
                                </button>
                                <button
                                  onClick={() => setConfirmSlug(null)}
                                  className="btn-ghost"
                                >
                                  No
                                </button>
                              </div>
                              {del.isError ? (
                                <span className="font-mono text-[11px] text-ember">
                                  {String(del.error?.message || del.error)}
                                </span>
                              ) : null}
                            </div>
                          ) : (
                            <div className="flex items-center justify-end gap-2">
                              {p.prompt_count > 0 ? (
                                <button
                                  onClick={() =>
                                    setPromptsSlug(
                                      isShowingPrompts ? null : p.slug
                                    )
                                  }
                                  title="The exact prompts this build sent the model"
                                  className={`btn-ghost ${
                                    isShowingPrompts
                                      ? "text-ember"
                                      : "text-ember/70 hover:text-ember"
                                  }`}
                                >
                                  Prompts
                                </button>
                              ) : null}
                              <button
                                onClick={() =>
                                  setImproveSlug(isImproving ? null : p.slug)
                                }
                                className={`btn-ghost ${
                                  isImproving
                                    ? "text-ember"
                                    : "text-ember/70 hover:text-ember"
                                }`}
                              >
                                Improve
                              </button>
                              <button
                                onClick={() => setConfirmSlug(p.slug)}
                                className="btn-ghost text-ember/70 hover:text-ember"
                              >
                                Delete
                              </button>
                            </div>
                          )}
                        </td>
                      </tr>
                      {isImproving ? (
                        <tr>
                          <td colSpan={12} className="p-0">
                            <ImproveInline slug={p.slug} stream={stream} />
                          </td>
                        </tr>
                      ) : null}
                      {isShowingPrompts ? (
                        <tr>
                          <td colSpan={12} className="p-0">
                            <PromptsInline slug={p.slug} />
                          </td>
                        </tr>
                      ) : null}
                    </React.Fragment>
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
