import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiFetch, apiPost } from "../api.js";
import {
  PageHeader,
  Panel,
  PanelHead,
  Pill,
  Empty,
  SignalGrid,
} from "../components/ui.jsx";
import {
  activeDeploymentIndex,
  canMoveDeploymentPointerBack,
  chooseDeployTarget,
  deploymentHealthLabel,
  safeDeploymentUrl,
} from "../deployWorkflow.js";
import { describeCostTruth } from "../costTruth.js";
import { projectBuildMetadata } from "../projectMetadata.js";
import { buildOutcome, summarizeBuildOutcomes } from "../buildOutcome.js";
import {
  canReverifyLocally,
  describeLocalReverify,
} from "../projectReverify.js";
import StreamStaleBanner from "../components/StreamStaleBanner.jsx";

function fmtMB(bytes) {
  if (bytes == null) return "—";
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

function fmtCost(usd) {
  return usd == null ? "—" : `$${Number(usd).toFixed(4)}`;
}

function aiEvidence(project) {
  return projectBuildMetadata(project);
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
  const toggleSort = () =>
    setSort((current) =>
      current.key === key
        ? { key, dir: current.dir === "asc" ? "desc" : "asc" }
        : { key, dir: defaultDir },
    );

  return (
    <th
      aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
      className={"px-2 py-1 font-normal " + (align === "right" ? "text-right" : "")}
    >
      <button
        type="button"
        onClick={toggleSort}
        className={
          "flex w-full items-center gap-1 rounded px-2 py-1 text-left hover:text-bone " +
          (align === "right" ? "justify-end" : "")
        }
        title={"Sort by " + label}
      >
        {label}
        <span aria-hidden="true" className={active ? "text-ember" : "text-ash/40"}>
          {arrow}
        </span>
      </button>
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
          aria-label={"Improvement goal for " + slug}
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

// Project feedback is distilled into reusable advisory lessons; it never starts an Improve run.
// It leaves the existing Serve/Stop controls untouched and is not raw project history.
function FeedbackInline({ slug, onClose }) {
  const [feedback, setFeedback] = useState("");
  const [category, setCategory] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [notice, setNotice] = useState("");

  async function submit(event) {
    event.preventDefault();
    const text = feedback.trim();
    if (!text) {
      setErr("Enter feedback before saving.");
      return;
    }
    if (busy) return;
    setBusy(true);
    setErr("");
    setNotice("");
    try {
      const result = await apiPost(
        "/projects/" + encodeURIComponent(slug) + "/feedback",
        { feedback: text, ...(category ? { category } : {}) },
      );
      setFeedback("");
      setCategory("");
      const captured = Number(result.captured) || 0;
      const deduped = Number(result.deduped) || 0;
      if (captured || deduped) {
        const parts = [];
        if (captured) {
          parts.push(
            String(captured) +
            " distilled lesson" +
            (captured === 1 ? "" : "s") +
            " captured"
          );
        }
        if (deduped) {
          parts.push(
            String(deduped) +
            " duplicate lesson" +
            (deduped === 1 ? "" : "s") +
            " already known"
          );
        }
        setNotice(parts.join(" · ") + ".");
      } else {
        setNotice("Feedback distilled into reusable advisory lessons.");
      }
    } catch (error) {
      setErr(String(error.message || error));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="bg-ink/30 px-4 py-3"
      aria-label={"Feedback for " + slug}
      onSubmit={submit}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="eyebrow text-[9px]">Project feedback · {slug}</div>
          <p className="mt-1 font-mono text-[11px] text-ash/70">
            Share what worked, what failed, or what should change. It is distilled into reusable advisory lessons.
          </p>
        </div>
        <button type="button" onClick={onClose} className="btn-ghost">
          Close
        </button>
      </div>
      <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(11rem,0.4fr)_minmax(20rem,1fr)]">
        <div>
          <label
            className="block font-mono text-[10px] text-ash"
            htmlFor={"feedback-category-" + slug}
          >
            Category <span className="text-ash/60">(optional)</span>
          </label>
          <select
            id={"feedback-category-" + slug}
            aria-label={"Feedback category for " + slug}
            className="field mt-2"
            value={category}
            onChange={(event) => setCategory(event.target.value)}
            disabled={busy}
          >
            <option value="">General (default)</option>
            <option value="visual">Visual</option>
            <option value="content">Content</option>
            <option value="usability">Usability</option>
            <option value="accessibility">Accessibility</option>
            <option value="performance">Performance</option>
          </select>
        </div>
        <div>
          <label
            className="block font-mono text-[10px] text-ash"
            htmlFor={"feedback-text-" + slug}
          >
            Feedback
          </label>
          <textarea
            id={"feedback-text-" + slug}
            aria-label={"Feedback for " + slug}
            className="field mt-2 min-h-24 resize-y"
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder="What worked, what failed, or what should change?"
            disabled={busy}
            maxLength={4000}
            required
          />
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
        <div aria-live="polite" className="font-mono text-[11px]">
          {err ? (
            <span role="alert" className="text-ember">{err}</span>
          ) : notice ? (
            <span role="status" className="text-plasma">{notice}</span>
          ) : (
            <span className="text-ash/60">Distilled into reusable advisory lessons, not raw project history.</span>
          )}
        </div>
        <button
          type="submit"
          disabled={busy || !feedback.trim()}
          className="btn-ember disabled:opacity-50"
        >
          {busy ? "Saving…" : "Save feedback"}
        </button>
      </div>
    </form>
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

function ServeCell({ slug, served, busy, err, canServe = true, serveReason = "", onServe, onStop }) {
  const running = !!served && (served.status === "running" || !!served.url);
  const disabledReason = canServe ? "" : serveReason || "no web entrypoint";
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
            disabled={!!busy || !canServe}
            className="btn-ghost text-plasma/80 hover:text-plasma disabled:opacity-50"
            title={disabledReason}
          >
            {busy === "serving" ? "Starting…" : canServe ? "Serve" : "No serve"}
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
      ) : disabledReason ? (
        <span
          className="max-w-[180px] truncate font-mono text-[10px] text-ash/45"
          title={disabledReason}
        >
          {disabledReason}
        </span>
      ) : null}
    </div>
  );
}

function ShipCell({ project, open, onToggle }) {
  const deployments = Array.isArray(project.deployments) ? project.deployments : [];
  const latest = deployments.length ? deployments[deployments.length - 1] : null;
  const liveUrl = safeDeploymentUrl(project.live_url || latest?.url || "");
  if (project.is_complete === false) {
    const label = project.delivery_state === "building" ? "Building" : "Not delivered";
    return (
      <span
        className="font-mono text-[10px] text-ash/50"
        title={project.serve_reason || "A completed build manifest is required"}
      >
        {label}
      </span>
    );
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
      {latest ? (
        <span className={`font-mono text-[10px] ${latest.ok ? "text-plasma" : "text-ember"}`}>
          {latest.provider || latest.target || "deploy"} · {latest.status || (latest.ok ? "succeeded" : "failed")}
        </span>
      ) : null}
      <button
        type="button"
        onClick={onToggle}
        className={`btn-ghost w-fit ${open ? "text-ember" : "text-plasma/80"}`}
        aria-expanded={open}
        aria-controls={`deploy-${project.slug}`}
      >
        {open ? "Close deploy" : "Deploy"}
      </button>
    </div>
  );
}

function ReverifyControl({ project, state, onRun }) {
  if (!canReverifyLocally(project)) return null;

  const busy = state?.status === "busy";
  const message = state?.message || "";
  return (
    <div className="flex max-w-[16rem] flex-col items-end gap-1" aria-live="polite">
      <button
        type="button"
        onClick={() => onRun(project.slug)}
        disabled={busy}
        className="btn-ghost whitespace-nowrap text-plasma/80 hover:text-plasma disabled:opacity-50"
        title="Run proof commands against the existing local files"
      >
        {busy ? "Reverifying locally..." : "Reverify locally"}
      </button>
      {state?.status !== "result" ? (
        <span className="font-mono text-[10px] text-ash/60">0 SkyN3t model calls</span>
      ) : null}
      {message ? (
        <span
          role={state.status === "error" ? "alert" : "status"}
          className={
            "max-w-full break-words font-mono text-[10px] " +
            (state.status === "error" ? "text-ember" : "text-plasma")
          }
          title={message}
        >
          {message}
        </span>
      ) : null}
    </div>
  );
}

function DeployInline({ project, onClose }) {
  const qc = useQueryClient();
  const slug = project.slug;
  const [target, setTarget] = useState("");
  const [confirmDeploy, setConfirmDeploy] = useState(false);
  const [confirmRollback, setConfirmRollback] = useState(false);

  const planQuery = useQuery({
    queryKey: ["deploy-plan", slug, target],
    queryFn: () => {
      const params = new URLSearchParams({ slug });
      if (target) params.set("target", target);
      return apiFetch(`/studio/deploy/plan?${params.toString()}`);
    },
    retry: 0,
  });
  const options = Array.isArray(planQuery.data?.provider_options)
    ? planQuery.data.provider_options
    : [];
  useEffect(() => {
    const next = chooseDeployTarget(options, target);
    if (next && next !== target) setTarget(next);
  }, [options, target]);

  const selected =
    planQuery.data?.preflight?.target === target
      ? planQuery.data.preflight
      : options.find((item) => item.target === target) || null;
  const deployments = Array.isArray(planQuery.data?.deployments)
    ? planQuery.data.deployments
    : Array.isArray(project.deployments)
      ? project.deployments
      : [];
  const currentLiveUrl = planQuery.data?.live_url || project.live_url || "";
  const activeIndex = activeDeploymentIndex(deployments, currentLiveUrl);
  const rollbackReady = canMoveDeploymentPointerBack(deployments, currentLiveUrl);

  const refreshEvidence = async () => {
    await Promise.all([
      planQuery.refetch(),
      qc.invalidateQueries({ queryKey: ["projects"] }),
    ]);
  };
  const deploy = useMutation({
    mutationFn: () => apiPost("/studio/deploy", { slug, target }),
    onSuccess: async () => {
      setConfirmDeploy(false);
      await refreshEvidence();
    },
  });
  const rollback = useMutation({
    mutationFn: () => apiPost("/studio/deploy/rollback", {
      slug,
      deployment_index: activeIndex,
      reason: "dashboard local-pointer rollback",
    }),
    onSuccess: async () => {
      setConfirmRollback(false);
      await refreshEvidence();
    },
  });

  const result = deploy.data?.result || null;
  const liveUrl = safeDeploymentUrl(result?.url || rollback.data?.to_url || currentLiveUrl);
  const health = deploy.data?.deploy_check || planQuery.data?.deploy_check || project.deploy_check || {};
  const healthLabel = deploymentHealthLabel(health);
  const blockers = Array.isArray(selected?.blockers) ? selected.blockers : [];
  const latestEvidence = deployments.length ? deployments[deployments.length - 1] : null;
  const evidenceStatus = result?.status || latestEvidence?.status || "not deployed";
  const evidenceOk = result ? Boolean(result.ok) : Boolean(latestEvidence?.ok);
  const healthDetail = health.reason ||
    (Array.isArray(health.issues) && health.issues.length ? health.issues.join(" · ") : "");

  return (
    <div id={`deploy-${slug}`} className="border-y border-hairline bg-void/25 px-4 py-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="eyebrow text-[9px]">Production deploy · {slug}</div>
        </div>
        <button type="button" onClick={onClose} className="btn-ghost">Close</button>
      </div>

      {planQuery.isLoading ? (
        <div className="mt-3 font-mono text-[11px] text-ash">Loading deploy preflight...</div>
      ) : planQuery.isError ? (
        <div className="mt-3 font-mono text-[11px] text-ember">
          {String(planQuery.error?.message || planQuery.error)}
        </div>
      ) : (
        <>
          <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(12rem,0.7fr)_minmax(20rem,1.3fr)]">
            <div className="space-y-2">
              <label className="block font-mono text-[10px] text-ash" htmlFor={`provider-${slug}`}>
                Provider
              </label>
              <select
                id={`provider-${slug}`}
                aria-label={`Deploy provider for ${slug}`}
                value={target}
                onChange={(event) => {
                  setTarget(event.target.value);
                  setConfirmDeploy(false);
                }}
                className="field"
              >
                {options.map((item) => (
                  <option key={item.target} value={item.target}>
                    {item.target}{item.ready ? " · ready" : " · setup needed"}
                  </option>
                ))}
              </select>
              {selected?.command ? (
                <div className="space-y-1 break-all font-mono text-[10px] text-ash/70">
                  {selected.build_command ? (
                    <div>
                      Build command ({selected.build_command_execution || "verified earlier; not rerun during deploy"}): {selected.build_command}
                    </div>
                  ) : null}
                  <div>Deploy command: {selected.command}</div>
                </div>
              ) : null}
              <Link to="/settings#deploy" className="font-mono text-[10px] text-plasma underline">
                Deploy settings
              </Link>
            </div>

            <div className="grid gap-1 sm:grid-cols-2">
              {[
                ["release", selected?.quality_gate?.passed, selected?.quality_gate?.passed ? "completed · GO · proof passed" : "release gate blocked"],
                ["remote gate", selected?.remote_allowed, selected?.remote_allowed ? "enabled" : "disabled"],
                ["credential", selected?.configured, selected?.configured ? "configured" : "missing"],
                ["provider CLI", selected?.cli_available, selected?.cli_available ? `${selected?.cli} installed` : `${selected?.cli || "provider"} missing`],
              ].map(([label, ok, value]) => (
                <div key={label} className="flex items-center justify-between border-b border-hairline/60 py-1.5 font-mono text-[10px]">
                  <span className="text-ash">{label}</span>
                  <span className={ok ? "text-plasma" : "text-ember"}>{value}</span>
                </div>
              ))}
            </div>
          </div>

          {blockers.length ? (
            <div className="mt-3 border-l-2 border-ember pl-3 font-mono text-[10px] text-ember">
              {blockers.join(" · ")}
            </div>
          ) : null}

          <div className="mt-4 flex flex-wrap items-center gap-2">
            {!confirmDeploy ? (
              <button
                type="button"
                onClick={() => setConfirmDeploy(true)}
                disabled={!selected?.ready || deploy.isPending}
                className="btn-ember disabled:opacity-40"
              >
                Review deploy
              </button>
            ) : (
              <>
                <span className="font-mono text-[11px] text-ember">
                  Deploy {slug} to {target} now?
                </span>
                <button
                  type="button"
                  onClick={() => deploy.mutate()}
                  disabled={deploy.isPending}
                  className="btn-ember disabled:opacity-40"
                >
                  {deploy.isPending ? "Deploying..." : `Deploy to ${target}`}
                </button>
                <button type="button" onClick={() => setConfirmDeploy(false)} className="btn-ghost">
                  Cancel
                </button>
              </>
            )}
          </div>
        </>
      )}

      {deploy.isError ? (
        <div className="mt-3 font-mono text-[11px] text-ember">
          {String(deploy.error?.message || deploy.error)}
        </div>
      ) : null}
      {result?.error ? (
        <div className="mt-2 font-mono text-[10px] text-ember">{result.error}</div>
      ) : null}
      {result || latestEvidence || liveUrl ? (
        <div className="mt-4 grid gap-2 border-t border-hairline pt-3 font-mono text-[10px] sm:grid-cols-4">
          <span>status <b className={evidenceOk ? "text-plasma" : "text-ember"}>{evidenceStatus}</b></span>
          <span>remote <b className="text-bone">{String(result?.remote_state || latestEvidence?.remote_state || "unknown")}</b></span>
          <span title={healthDetail}>health <b className={health.ok ? "text-plasma" : healthLabel === "unhealthy" ? "text-ember" : "text-ash"}>{healthLabel}</b></span>
          {liveUrl ? <a href={liveUrl} target="_blank" rel="noopener noreferrer" className="truncate text-plasma underline">live URL</a> : <span>live URL unavailable</span>}
        </div>
      ) : null}

      <div className="mt-4 border-t border-hairline pt-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="eyebrow text-[9px]">Deployment history</div>
            <div className="mt-1 font-mono text-[10px] text-ash/70">
              Rollback below changes only SkyN3t's local live-URL pointer. Provider traffic is not changed.
            </div>
          </div>
          {!confirmRollback ? (
            <button
              type="button"
              onClick={() => setConfirmRollback(true)}
              disabled={!rollbackReady || rollback.isPending}
              className="btn-ghost disabled:opacity-40"
              title={rollbackReady ? "Move local pointer to the previous successful URL" : "Two successful deployments are required"}
            >
              Move pointer back
            </button>
          ) : (
            <div className="flex items-center gap-2">
              <span className="font-mono text-[10px] text-ember">Local pointer only. Continue?</span>
              <button type="button" onClick={() => rollback.mutate()} className="btn-ember">Confirm</button>
              <button type="button" onClick={() => setConfirmRollback(false)} className="btn-ghost">Cancel</button>
            </div>
          )}
        </div>
        {rollback.isError ? (
          <div className="mt-2 font-mono text-[10px] text-ember">{String(rollback.error?.message || rollback.error)}</div>
        ) : rollback.data?.error ? (
          <div className="mt-2 font-mono text-[10px] text-ember">{rollback.data.error}</div>
        ) : rollback.data?.note ? (
          <div className="mt-2 font-mono text-[10px] text-plasma">{rollback.data.note}</div>
        ) : null}
        <div className="mt-2 max-h-40 overflow-auto border-t border-hairline/60">
          {deployments.length ? deployments.slice().reverse().map((item, reverseIndex) => {
            const index = deployments.length - reverseIndex - 1;
            return (
              <div key={`${item.ts || index}-${index}`} className="grid gap-1 border-b border-hairline/50 py-1.5 font-mono text-[10px] sm:grid-cols-[8rem_7rem_minmax(10rem,1fr)_8rem]">
                <span className="text-ash">{fmtDate(item.ts ? item.ts * 1000 : null)}</span>
                <span className={item.ok ? "text-plasma" : "text-ember"}>{item.provider || item.target || "unknown"} · {item.status || "unknown"}</span>
                {safeDeploymentUrl(item.url) ? <a href={safeDeploymentUrl(item.url)} target="_blank" rel="noopener noreferrer" className="truncate text-bone underline">{item.url}</a> : <span className="text-ash/60">no URL</span>}
                <span className="text-ash/70">{item.manifest_pointer_active ? "active pointer" : item.remote_state || "not active"}</span>
              </div>
            );
          }) : (
            <div className="py-2 font-mono text-[10px] text-ash/60">No deployment attempts recorded.</div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function Projects({ stream }) {
  const qc = useQueryClient();
  const [confirmSlug, setConfirmSlug] = useState(null);
  const [improveSlug, setImproveSlug] = useState(null);
  const [promptsSlug, setPromptsSlug] = useState(null);
  const [deploySlug, setDeploySlug] = useState(null);
  const [feedbackSlug, setFeedbackSlug] = useState(null);
  const [busy, setBusy] = useState({}); // slug -> "serving" | "stopping"
  const [serveErr, setServeErr] = useState({}); // slug -> message
  const [reverifyState, setReverifyState] = useState({});
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

  async function reverifyLocally(slug) {
    setReverifyState((current) => ({
      ...current,
      [slug]: { status: "busy", message: "" },
    }));
    try {
      const result = await apiPost(
        `/projects/${encodeURIComponent(slug)}/reverify`,
        {},
      );
      setReverifyState((current) => ({
        ...current,
        [slug]: { status: "result", message: describeLocalReverify(result) },
      }));
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["projects"] }),
        qc.invalidateQueries({ queryKey: ["builds"] }),
      ]);
    } catch (error) {
      setReverifyState((current) => ({
        ...current,
        [slug]: {
          status: "error",
          message: `Local reverify failed: ${String(error.message || error)}`,
        },
      }));
    }
  }

  const projects = Array.isArray(data) ? data : data?.projects || [];
  const liveCount = Object.keys(served).length;
  const outcomeSummary = summarizeBuildOutcomes(projects);
  const projectSignals = [
    { label: "projects", value: String(projects.length) },
    { label: "live serves", value: String(liveCount) },
    { label: "delivered", value: String(outcomeSummary.delivered) },
    { label: "shippable", value: String(outcomeSummary.shippable) },
    {
      label: "not delivered",
      value: String(outcomeSummary.notDelivered),
      title: `${outcomeSummary.active} still building; cancelled and failed rows remain explicit below`,
    },
    { label: "cancelled", value: String(outcomeSummary.cancelled) },
    { label: "failed", value: String(outcomeSummary.failed) },
    {
      label: "non-shippable spend",
      value: outcomeSummary.unknownCostRows > 0
        ? outcomeSummary.nonShippableSpendUsd > 0
          ? `>= ${fmtCost(outcomeSummary.nonShippableSpendUsd)} + ${outcomeSummary.unknownCostRows} unknown`
          : `${outcomeSummary.unknownCostRows} unknown`
        : fmtCost(outcomeSummary.nonShippableSpendUsd),
      title: outcomeSummary.unknownCostRows > 0
        ? "Known recorded spend is a lower bound; one or more CLI-backed terminal runs have unknown provider-account cost"
        : "Recorded build cost for terminal projects with no shippable outcome; provider cost truth may still be estimated",
    },
  ];
  const visibleProjectSignals = isLoading
    ? projectSignals.map((item) => ({ ...item, value: "…" }))
    : projectSignals;

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
              {isLoading
                ? "loading projects…"
                : projects.length + " project" + (projects.length !== 1 ? "s" : "")}
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
        <SignalGrid label="Projects cockpit" items={visibleProjectSignals} />
      </Panel>

      <CleanupPanel qc={qc} />

      {/* dead stream: the serve/improve columns replay a frozen event buffer */}
      <StreamStaleBanner stream={stream} />

      <Panel className="overflow-hidden">
        <PanelHead
          label="Project list"
          right={
            <span className="font-mono text-[11px] text-ash">
              {isLoading ? "loading…" : projects.length + " total"}
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
                  <SortHeader label="LLM cost" sortKey="cost_usd" sort={sort} setSort={setSort} />
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
                  const isDeploying = deploySlug === p.slug;
                  const isGivingFeedback = feedbackSlug === p.slug;
                  const ai = aiEvidence(p);
                  const costTruth = describeCostTruth(p);
                  const outcome = buildOutcome(p);
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
                          <Pill tone={outcome.tone}>{outcome.label}</Pill>
                          <div
                            className="mt-1 max-w-[11rem] font-mono text-[10px] text-ash/70"
                            title={outcome.title}
                          >
                            {outcome.detail}
                          </div>
                          {p.local_reverify?.proof_passed ? (
                            <div
                              className="mt-1 max-w-[13rem] font-mono text-[10px] text-plasma/80"
                              title={p.local_reverify.reason || "Local proof passed"}
                            >
                              local proof passed{p.local_reverify.review_refreshed ? " · re-reviewed" : ""}{p.local_reverify.promoted ? " · promoted" : " · fresh review needed"}
                            </div>
                          ) : null}
                        </td>
                        <td className="px-4 py-2 font-mono text-xs text-ash">
                          {p.score ?? "—"}
                        </td>
                        <td className="px-4 py-2 font-mono text-[11px] text-ash">
                          <div className="max-w-[15rem] truncate text-bone" title={ai.title}>
                            {ai.profile} · {ai.backend}
                          </div>
                          <div className="max-w-[15rem] truncate text-ash/80" title={ai.title}>
                            {ai.modelSource} · {ai.model}
                          </div>
                          <div className="whitespace-nowrap text-ash/70" title={ai.title}>
                            prompts {ai.promptCount ?? "—"} · stages {ai.stageCount ?? "—"}
                          </div>
                          <div className="whitespace-nowrap text-ash/70" title={ai.title}>
                            run {ai.runCostLabel} · stage {ai.stageCostLabel}
                          </div>
                          <div className="whitespace-nowrap text-ash/60" title={ai.title}>
                            skills {ai.skillCount ?? "—"} · recall {ai.recallCount ?? "—"} · roles {ai.roleStages}
                          </div>
                        </td>
                        <td className="px-4 py-2 font-mono text-xs">
                          <span
                            className={p.wasted_usd ? "text-ember" : "text-ash"}
                            title={costTruth.title}
                          >
                            {costTruth.amountLabel || fmtCost(p.cost_usd)}
                          </span>
                          {costTruth.label ? (
                            <span
                              className={
                                "ml-1 text-[10px] " +
                                (costTruth.classification === "provider_confirmed"
                                  ? "text-plasma"
                                  : costTruth.classification === "estimate"
                                    ? "text-bone/70"
                                    : "text-ember/70")
                              }
                              title={costTruth.title}
                            >
                              {costTruth.label}
                            </span>
                          ) : null}
                          {costTruth.externalUnknown ? (
                            <div className="text-[10px] text-ember/70" title={costTruth.title}>
                              + assets unknown
                            </div>
                          ) : null}
                          {Number(p.wasted_usd) > 0 ? (
                            <span
                              className="ml-1 text-[10px] text-ember/70"
                              title="The API explicitly recorded this amount as wasted spend"
                            >
                              recorded waste
                            </span>
                          ) : null}
                        </td>
                        <td
                          className="px-4 py-2 font-mono text-xs text-ash"
                          title={`${p.file_count ?? 0} files · internal .preview excluded`}
                        >
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
                            canServe={p.has_serve !== false}
                            serveReason={p.serve_reason}
                            onServe={serve}
                            onStop={stopServe}
                          />
                        </td>
                        <td className="px-4 py-2">
                          <ShipCell
                            project={p}
                            open={isDeploying}
                            onToggle={() => setDeploySlug(isDeploying ? null : p.slug)}
                          />
                        </td>
                        <td className="px-4 py-2">
                          <div className="flex items-center gap-3">
                            {p.has_preview ? (
                              <a
                                href={p.preview_url}
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
                            <div className="flex flex-wrap items-start justify-end gap-2">
                              <ReverifyControl
                                project={p}
                                state={reverifyState[p.slug]}
                                onRun={reverifyLocally}
                              />
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
                              {p.is_complete !== false ? (
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
                              ) : null}
                              <button
                                type="button"
                                onClick={() =>
                                  setFeedbackSlug(isGivingFeedback ? null : p.slug)
                                }
                                className={
                                  "btn-ghost " +
                                  (isGivingFeedback
                                    ? "text-ember"
                                    : "text-plasma/80 hover:text-plasma")
                                }
                                aria-expanded={isGivingFeedback}
                                aria-controls={"feedback-" + p.slug}
                              >
                                {isGivingFeedback ? "Close feedback" : "Feedback"}
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
                      {isDeploying ? (
                        <tr>
                          <td colSpan={12} className="p-0">
                            <DeployInline project={p} onClose={() => setDeploySlug(null)} />
                          </td>
                        </tr>
                      ) : null}
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
                      {isGivingFeedback ? (
                        <tr id={"feedback-" + p.slug}>
                          <td colSpan={12} className="p-0">
                            <FeedbackInline
                              slug={p.slug}
                              onClose={() => setFeedbackSlug(null)}
                            />
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
