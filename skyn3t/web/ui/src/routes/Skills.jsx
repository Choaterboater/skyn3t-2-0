import React, { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiPost, queryFn } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill, Empty, ErrorText } from "../components/ui.jsx";
import { filterSkills, skillFacets, sortSkills } from "../skillBrowser.js";

function SummaryChips({ title, items }) {
  const entries = Object.entries(items || {}).sort((a, b) => b[1] - a[1]).slice(0, 8);
  if (!entries.length) return null;
  return (
    <div>
      <div className="eyebrow mb-2">{title}</div>
      <div className="flex flex-wrap gap-1.5">
        {entries.map(([name, count]) => (
          <Pill key={name} tone={title === "Risk" && name === "medium" ? "ember" : "plasma"}>
            {name} · {count}
          </Pill>
        ))}
      </div>
    </div>
  );
}

const QUARANTINE_TAGS = new Set(["hygiene:quarantine", "quarantine", "disabled"]);

function tagsFor(skill) {
  return Array.isArray(skill?.tags) ? skill.tags.map((tag) => String(tag).trim().toLowerCase()) : [];
}

function isQuarantined(skill) {
  return skill?.quarantined === true || tagsFor(skill).some((tag) => QUARANTINE_TAGS.has(tag));
}

function countOrFallback(value, fallback) {
  const count = Number(value);
  return Number.isFinite(count) && count >= 0 ? count : fallback;
}
const CAPABILITY_STATUS_LABELS = {
  active: "active",
  inactive: "not active",
  rolled_back: "rolled back",
  unknown: "capability state not recorded",
};

function capabilityStatusTone(status) {
  if (status === "active") return "plasma";
  if (status === "rolled_back") return "ember";
  return "ash";
}

function EvaluationRow({ evaluation }) {
  const created = evaluation.created_at || evaluation.timestamp;
  const parsed = created ? new Date(created) : null;
  const effect = String(evaluation.effectiveness || "not_checked");
  const compatibility = String(evaluation.status || "not_checked");
  return (
    <li className="border-b border-hairline/60 py-2 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2">
        <Pill tone={effect === "passed" ? "plasma" : effect === "failed" ? "ember" : "ash"}>
          effectiveness · {effect.replaceAll("_", " ")}
        </Pill>
        <Pill tone={compatibility === "passed" ? "plasma" : compatibility === "failed" ? "ember" : "ash"}>
          static checks · {compatibility.replaceAll("_", " ")}
        </Pill>
        <span className="font-mono text-[10px] text-ash/70">
          {parsed && !Number.isNaN(parsed.getTime()) ? parsed.toLocaleString() : "date not recorded"}
        </span>
      </div>
      {evaluation.reason ? <p className="mt-1 text-xs text-ash">{evaluation.reason}</p> : null}
      {evaluation.receipt ? (
        <p className="mt-1 break-words font-mono text-[10px] text-ash/70">receipt: {JSON.stringify(evaluation.receipt)}</p>
      ) : null}
    </li>
  );
}

function SkillLifecyclePanel({
  slug,
  skillName,
  capabilityStatus,
  expanded,
  onToggle,
  evaluations,
  evaluationsLoading,
  evaluationsError,
  evaluate,
  activate,
  rollback,
  activationReason,
  setActivationReason,
  rollbackReason,
  setRollbackReason,
}) {
  const capability = capabilityStatus && typeof capabilityStatus === "object" ? capabilityStatus : {};
  const normalizedStatus = capability.active ? "active" : capability.status || "unknown";
  return (
    <div className="border-t border-hairline pt-2">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="font-mono text-[11px] text-ash underline decoration-dotted underline-offset-4 hover:text-bone"
      >
        {expanded ? "Hide evidence and overrides" : "Evidence / manual overrides"}
      </button>
      {expanded ? (
        <div className="mt-2 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Pill tone={capabilityStatusTone(normalizedStatus)}>
              capability · {CAPABILITY_STATUS_LABELS[normalizedStatus] || normalizedStatus}
            </Pill>
            <button
              type="button"
              className="btn-ghost py-0.5 text-[10px]"
              disabled={evaluate.isPending || !slug}
              onClick={() => evaluate.mutate(slug)}
              title="Refresh local static checks; this is not an effectiveness test"
            >
              {evaluate.isPending ? "Evaluating…" : "Evaluate"}
            </button>
          </div>
          {evaluate.isError ? <ErrorText className="max-h-20 text-[10px]">{String(evaluate.error?.message || evaluate.error)}</ErrorText> : null}
          {evaluationsLoading ? (
            <p className="font-mono text-[10px] text-ash/70">loading evaluation history…</p>
          ) : evaluationsError ? (
            <ErrorText className="max-h-20 text-[10px]">{String(evaluationsError.message || evaluationsError)}</ErrorText>
          ) : Array.isArray(evaluations) && evaluations.length ? (
            <div>
              <p className="font-mono text-[10px] text-ash/70">Evaluation history ({evaluations.length})</p>
              <ul>
                {evaluations.map((evaluation, index) => (
                  <EvaluationRow key={evaluation.id || index} evaluation={evaluation} />
                ))}
              </ul>
            </div>
          ) : (
            <p className="font-mono text-[10px] text-ash/70">No evaluation history recorded.</p>
          )}
          <label className="block text-xs text-ash">
            <span className="mb-1 block font-mono text-[10px] uppercase">Activation reason (optional)</span>
            <input
              type="text"
              className="field"
              value={activationReason}
              onChange={(event) => setActivationReason(event.target.value)}
              aria-label={`Activation reason for ${skillName}`}
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-ember py-0.5 text-[10px]"
              disabled={activate.isPending || !slug || !capability.can_activate}
              onClick={() => activate.mutate({ slug, reason: activationReason.trim() })}
            >
              {activate.isPending ? "Activating…" : "Activate"}
            </button>
            {activate.isError ? <ErrorText className="max-h-16 text-[10px]">{String(activate.error?.message || activate.error)}</ErrorText> : null}
          </div>
          <label className="block text-xs text-ash">
            <span className="mb-1 block font-mono text-[10px] uppercase">Rollback reason (required)</span>
            <input
              type="text"
              className="field"
              value={rollbackReason}
              onChange={(event) => setRollbackReason(event.target.value)}
              aria-label={`Rollback reason for ${skillName}`}
            />
          </label>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-ghost py-0.5 text-[10px]"
              disabled={rollback.isPending || !slug || !rollbackReason.trim()}
              title={rollbackReason.trim() ? "Deactivate this capability" : "A rollback reason is required"}
              onClick={() => rollback.mutate({ slug, reason: rollbackReason.trim() })}
            >
              {rollback.isPending ? "Rolling back…" : "Roll back"}
            </button>
            {rollback.isError ? <ErrorText className="max-h-20 text-[10px]">{String(rollback.error?.message || rollback.error)}</ErrorText> : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

export default function Skills() {
  const queryClient = useQueryClient();
  const [catalogPath, setCatalogPath] = useState("");
  const [catalogResult, setCatalogResult] = useState(null);
  const [catalogError, setCatalogError] = useState("");
  const [promotionResult, setPromotionResult] = useState(null);
  const [expandedSkill, setExpandedSkill] = useState(null);
  const [rollbackReason, setRollbackReason] = useState("");
  const [activationReason, setActivationReason] = useState("");
  const [filters, setFilters] = useState({ search: "", category: "", topic: "", status: "", rating: "", budget: "" });
  const [browseMode, setBrowseMode] = useState("categories");
  const [sort, setSort] = useState("rating");
  const [visibleCount, setVisibleCount] = useState(36);

  const evaluationsQuery = useQuery({
    queryKey: ["skill-evaluations", expandedSkill],
    queryFn: () => apiFetch(`/skills/${encodeURIComponent(expandedSkill)}/evaluations`),
    enabled: Boolean(expandedSkill),
    retry: 0,
  });
  const evaluations = expandedSkill ? (Array.isArray(evaluationsQuery.data?.evaluations) ? evaluationsQuery.data.evaluations : []) : [];
  const evaluationsLoading = evaluationsQuery.isLoading || evaluationsQuery.isFetching;
  const evaluationsError = evaluationsQuery.isError ? evaluationsQuery.error : null;
  const { data, isLoading, error } = useQuery({
    queryKey: ["skills"],
    queryFn: queryFn("/skills"),
    refetchInterval: 15000,
  });

  const skills = Array.isArray(data) ? data : data?.skills || [];
  const matchingSkills = useMemo(() => sortSkills(filterSkills(skills, filters), sort), [skills, filters, sort]);
  const categoryFacets = useMemo(() => skillFacets(matchingSkills, "categories"), [matchingSkills]);
  const topicFacets = useMemo(
    () => skillFacets(filterSkills(skills, { ...filters, topic: "" }), "topics"),
    [skills, filters],
  );
  const updateFilter = (key, value) => {
    setFilters((current) => ({ ...current, [key]: value }));
    setVisibleCount(36);
  };
  const clearFilters = () => {
    setFilters({ search: "", category: "", topic: "", status: "", rating: "", budget: "" });
    setVisibleCount(36);
    setBrowseMode("categories");
  };
  const chooseCategory = (category) => {
    setFilters((current) => ({ ...current, category }));
    setVisibleCount(36);
    setBrowseMode("skills");
  };
  const patterns = Array.isArray(data?.patterns) ? data.patterns : [];
  const skillSummary = data?.summary || {};
  const catalogSummary = catalogResult?.summary || {};
  const catalogEntries = Array.isArray(catalogResult?.entries) ? catalogResult.entries : [];
  const quarantinedFallback = skills.filter(isQuarantined).length;
  const registeredCount = countOrFallback(skillSummary.registered, skills.length);
  const activeCount = countOrFallback(skillSummary.active, skills.length - quarantinedFallback);
  const quarantinedCount = countOrFallback(skillSummary.quarantined, quarantinedFallback);
  const reviewRequiredCount = countOrFallback(
    skillSummary.review_required,
    skills.filter((skill) => skill.admission_check?.status === "review_required").length,
  );
  const promotionReadyCount = countOrFallback(
    skillSummary.promotion_ready,
    skills.filter((skill) => skill?.promotion_ready === true).length,
  );

  const previewCatalog = useMutation({
    mutationFn: async () => {
      const path = catalogPath.trim();
      if (!path) throw new Error("Catalog path is required");
      return apiFetch(`/agent-catalog?path=${encodeURIComponent(path)}&limit=100`);
    },
    onSuccess: (res) => {
      setCatalogError("");
      setCatalogResult(res);
    },
    onError: (err) => {
      setCatalogError(String(err.message || err));
    },
  });

  const promoteExternal = useMutation({
    mutationFn: (slug) => apiPost(`/skills/${encodeURIComponent(slug)}/promote`, {}),
    onSuccess: async (res) => {
      setPromotionResult({
        promoted: Boolean(res?.promoted),
        message: res?.message || "Promotion did not return a status.",
      });
      if (res?.promoted) {
        await queryClient.invalidateQueries({ queryKey: ["skills"] });
      }
    },
    onError: (err) => {
      setPromotionResult({ promoted: false, message: String(err.message || err) });
    },
  });
  const evaluateSkill = useMutation({
    mutationFn: (slug) => apiPost(`/skills/${encodeURIComponent(slug)}/evaluate`, {}),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["skill-evaluations", expandedSkill] });
      queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
  const activateSkill = useMutation({
    mutationFn: ({ slug, reason }) => apiPost(`/skills/${encodeURIComponent(slug)}/activate`, reason ? { reason } : {}),
    onSuccess: () => {
      setActivationReason("");
      queryClient.invalidateQueries({ queryKey: ["skill-evaluations", expandedSkill] });
      queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
  const rollbackSkill = useMutation({
    mutationFn: ({ slug, reason }) => apiPost(`/skills/${encodeURIComponent(slug)}/rollback`, { reason }),
    onSuccess: () => {
      setRollbackReason("");
      queryClient.invalidateQueries({ queryKey: ["skill-evaluations", expandedSkill] });
      queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
  const toggleSkillDetail = (slug) => {
    setExpandedSkill((current) => (current === slug ? null : slug));
    setRollbackReason("");
    setActivationReason("");
  };
  const acceptAllReady = useMutation({
    mutationFn: () => apiPost("/skills/promote-ready", {}),
    onSuccess: async (res) => {
      const accepted = Array.isArray(res?.promoted) ? res.promoted.length : 0;
      setPromotionResult({
        promoted: accepted > 0,
        message: res?.message || `Accepted ${accepted} ready skills.`,
      });
      await queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (err) => {
      setPromotionResult({ promoted: false, message: String(err.message || err) });
    },
  });
  const importCatalog = useMutation({
    mutationFn: async () => {
      const path = catalogPath.trim();
      if (!path) throw new Error("Catalog path is required");
      return apiPost("/agent-catalog/import", { path, limit: 100 });
    },
    onSuccess: async (res) => {
      setCatalogError("");
      setCatalogResult((current) => ({ ...(current || {}), ...res }));
      await queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
    onError: (err) => {
      setCatalogError(String(err.message || err));
    },
  });

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Capability Library"
        title="Skills"
        sub="Background improvement discovers and checks advisory skills, activates eligible ones, then attempts proven local feature changes. Static checks do not prove effectiveness. Failed and rolled-back skills stay blocked."
        actions={
          <span className="badge border-hairline text-ash">
            registered · <span className="ml-1 text-plasma">{registeredCount}</span>
          </span>
        }
      />

      {error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Skills hub unreachable: {String(error.message)}
        </Panel>
      ) : null}

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Registered" value={registeredCount} tone="plasma" hint="all records" />
        <Stat label="Active" value={activeCount} tone="plasma" hint="eligible to inject" />
        <Stat
          label="Quarantined"
          value={quarantinedCount}
          tone={quarantinedCount ? "ember" : "bone"}
          hint="held by admission or evaluation"
        />
        <Stat
          label="Ready to promote"
          value={promotionReadyCount}
          tone={promotionReadyCount ? "ember" : "bone"}
          hint="passes promotion gate"
        />
        <Stat label="Needs review" value={reviewRequiredCount} tone={reviewRequiredCount ? "ember" : "bone"} hint="held by on-add checks" />
        <Stat
          label="Patterns"
          value={patterns.length}
          tone={patterns.length ? "ember" : "bone"}
          hint="build shapes"
        />
        <Stat
          label="Catalog"
          value={catalogSummary.entries ?? 0}
          tone={catalogSummary.entries ? "plasma" : "bone"}
          hint="previewed roles"
        />
      </div>

      {promotionResult ? (
        <Panel
          className={`mb-6 p-4 text-sm ${promotionResult.promoted ? "border-plasma/40 text-plasma" : "border-ember/40 text-ember"}`}
        >
          <span aria-live="polite">{promotionResult.message}</span>
        </Panel>
      ) : null}
      <Panel className="mb-6">
        <PanelHead
          label="Agent catalog"
          right={<span className="font-mono text-[11px] text-ash">/agent-catalog</span>}
        />
        <div className="grid grid-cols-1 gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_auto_auto]">
          <input
            className="field"
            aria-label="Agent catalog path"
            placeholder="/path/to/agents-or-skills"
            value={catalogPath}
            onChange={(event) => setCatalogPath(event.target.value)}
          />
          <button
            type="button"
            className="btn-ghost"
            disabled={previewCatalog.isPending}
            onClick={() => previewCatalog.mutate()}
          >
            Preview
          </button>
          <button
            type="button"
            className="btn-ember"
            disabled={importCatalog.isPending}
            onClick={() => importCatalog.mutate()}
          >
            Import
          </button>
        </div>
        {catalogError ? (
          <div className="border-t border-hairline px-4 py-3 font-mono text-xs text-ember">
            {catalogError}
          </div>
        ) : null}
        {catalogResult ? (
          <div className="grid grid-cols-1 gap-4 border-t border-hairline p-4 lg:grid-cols-4">
            <div>
              <div className="eyebrow mb-2">Path</div>
              <div className="truncate font-mono text-xs text-ash">{catalogResult.path}</div>
              {catalogResult.imported != null ? (
                <div className="mt-2 font-mono text-xs text-plasma">
                  imported {catalogResult.imported}
                </div>
              ) : null}
            </div>
            <SummaryChips title="Stacks" items={catalogSummary.by_stack} />
            <SummaryChips title="Stages" items={catalogSummary.by_stage} />
            <SummaryChips title="Risk" items={catalogSummary.by_risk} />
          </div>
        ) : null}
        {catalogEntries.length ? (
          <div className="max-h-72 overflow-auto border-t border-hairline">
            <table className="w-full text-left font-mono text-xs">
              <thead>
                <tr className="border-b border-hairline">
                  <th className="eyebrow px-4 py-3 font-normal">Role</th>
                  <th className="eyebrow px-4 py-3 font-normal">Stages</th>
                  <th className="eyebrow px-4 py-3 font-normal">Stacks</th>
                  <th className="eyebrow px-4 py-3 font-normal">Risk</th>
                </tr>
              </thead>
              <tbody>
                {catalogEntries.slice(0, 40).map((entry) => (
                  <tr key={entry.id} className="border-b border-hairline/60">
                    <td className="px-4 py-2 text-bone">
                      <div>{entry.title}</div>
                      <div className="truncate text-ash/70">{entry.source_path}</div>
                    </td>
                    <td className="px-4 py-2 text-ash">{(entry.stages || []).join(", ")}</td>
                    <td className="px-4 py-2 text-ash">{(entry.stacks || []).join(", ")}</td>
                    <td className="px-4 py-2">
                      <Pill tone={entry.risk === "medium" ? "ember" : "plasma"}>{entry.risk}</Pill>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </Panel>

      <Panel>
        <PanelHead
          label="Skill index"
          right={
            <div className="flex items-center gap-2">
              <span className="font-mono text-[11px] text-ash">/skills</span>
              <button
                type="button"
                className="btn-ember"
                disabled={acceptAllReady.isPending || promotionReadyCount === 0}
                aria-label={`Accept all ${promotionReadyCount} ready skills`}
                onClick={() => {
                  setPromotionResult(null);
                  acceptAllReady.mutate();
                }}
              >
                {acceptAllReady.isPending ? "Accepting…" : `Accept all${promotionReadyCount ? ` · ${promotionReadyCount}` : ""}`}
              </button>
            </div>
          }
        />
        <div className="space-y-3 border-b border-hairline p-4">
          <div className="flex flex-wrap gap-2">
            <label className="min-w-48 flex-1 text-xs text-ash">
              <span className="mb-1 block">Search titles, descriptions, categories and topics</span>
              <input
                type="search"
                className="field"
                aria-label="Search skills"
                placeholder="MCP, desktop editor, testing, authentication..."
                value={filters.search}
                onChange={(event) => updateFilter("search", event.target.value)}
              />
            </label>
            <button type="button" className="btn-ghost self-end" onClick={clearFilters}>Clear filters</button>
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <label className="text-xs text-ash">
              Topic
              <select className="field mt-1" aria-label="Skill topic" value={filters.topic} onChange={(event) => updateFilter("topic", event.target.value)}>
                <option value="">All matching topics</option>
                {filters.topic && !topicFacets.some(([topic]) => topic === filters.topic) ? <option value={filters.topic}>{filters.topic} (0)</option> : null}
                {topicFacets.map(([topic, count]) => <option key={topic} value={topic}>{topic} ({count})</option>)}
              </select>
            </label>
            <label className="text-xs text-ash">
              Status
              <select className="field mt-1" aria-label="Skill status" value={filters.status} onChange={(event) => updateFilter("status", event.target.value)}>
                <option value="">All registered</option>
                <option value="active">Active</option>
                <option value="quarantined">Quarantined</option>
                <option value="review_required">Needs review</option>
                <option value="not_checked">Check missing / failed</option>
              </select>
            </label>
            <label className="text-xs text-ash">
              Prompt readiness
              <select className="field mt-1" aria-label="Skill rating" value={filters.rating} onChange={(event) => updateFilter("rating", event.target.value)}>
                <option value="">All ratings</option>
                <option value="4">4/5 - compact procedure</option>
                <option value="3">3/5 - useful with gaps</option>
                <option value="2">2/5 - needs distillation</option>
                <option value="1">1/5 - unsuitable as-is</option>
              </select>
            </label>
            <label className="text-xs text-ash">
              Prompt size
              <select className="field mt-1" aria-label="Skill prompt size" value={filters.budget} onChange={(event) => updateFilter("budget", event.target.value)}>
                <option value="">Any size</option>
                <option value="fits">Fits alone in global budget</option>
                <option value="oversized">Exceeds global budget</option>
              </select>
            </label>
            <label className="text-xs text-ash">
              Sort
              <select className="field mt-1" aria-label="Sort skills" value={sort} onChange={(event) => { setSort(event.target.value); setVisibleCount(36); }}>
                <option value="rating">Readiness: highest first</option>
                <option value="name">Name</option>
                <option value="size">Smallest prompt first</option>
              </select>
            </label>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              className="btn-ghost"
              aria-pressed={browseMode === "categories"}
              onClick={() => { updateFilter("category", ""); setBrowseMode("categories"); }}
            >Browse categories</button>
            <button type="button" className="btn-ghost" aria-pressed={browseMode === "skills"} onClick={() => setBrowseMode("skills")}>Show skills</button>
            {filters.category ? <Pill tone="plasma">{filters.category}</Pill> : null}
            <span className="text-xs text-ash" aria-live="polite" data-testid="skill-match-count">
              {matchingSkills.length} matching of {skills.length} registered
            </span>
          </div>
          <p className="text-xs text-ash">
            Ratings are provisional text-only prompt-readiness checks, not measured effectiveness.
            A 5/5 is reserved for independent review. Categories organize browsing; they do not activate skills or change build eligibility.
            Size is per skill, before shared wrapper text and other selected advice.
          </p>
        </div>
        {isLoading ? (
          <Empty icon="≋">Loading the library…</Empty>
        ) : skills.length === 0 ? (
          <Empty icon="◇">No skills registered. The swarm has nothing learned yet.</Empty>
        ) : matchingSkills.length === 0 ? (
          <Empty icon="◇">No skills match these filters. Clear filters to browse the full library.</Empty>
        ) : browseMode === "categories" ? (
          <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3" aria-label="Matching skill categories">
            {categoryFacets.map(([category, count]) => (
              <button key={category} type="button" className="panel bg-void/40 p-4 text-left hover:border-plasma/50" onClick={() => chooseCategory(category)}>
                <span className="block font-display text-base font-bold text-bone">{category}</span>
                <span className="mt-1 block text-xs text-ash">{count} matching skills</span>
              </button>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
            {matchingSkills.slice(0, visibleCount).map((s) => {
              const tags = tagsFor(s);
              const quarantined = isQuarantined(s);
              const externalCandidate = tags.includes("external-candidate");
              const legacyMigrated = tags.includes("legacy-migrated");
              const provenanceComplete = s.provenance_complete === true;
              const skillName = s.name || s.title || s.slug || "Untitled skill";
              const capability = s.capability_status && typeof s.capability_status === "object" ? s.capability_status : {};
              const evaluationStatus = capability.status || "not_checked";
              const evaluationTone = evaluationStatus === "passed" ? "plasma" : evaluationStatus === "failed" ? "ember" : "ash";
              const admission = s.admission_check || { status: "not_checked" };
              return (
                <div
                  key={s.slug || s.name || s.id}
                  className={`panel flex flex-col gap-3 bg-void/40 p-4 transition-all duration-300 ${evaluationStatus === "passed" ? "border-plasma/40" : evaluationStatus === "failed" ? "border-ember/40" : "border-hairline"}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="font-display text-base font-bold text-bone">
                      {skillName}
                    </h3>
                    {s.content_audit?.rating != null ? (
                      <span className="whitespace-nowrap font-mono text-sm text-plasma" title="Provisional prompt-readiness rating">{s.content_audit.rating}/5</span>
                    ) : null}
                  </div>

                  {s.description ? (
                    <p className="text-sm text-ash">{s.description}</p>
                  ) : null}
                  <div className="text-xs text-ash">
                    <Pill tone={admission.status === "passed" ? "plasma" : "ember"}>
                      On-add check: {admission.status.replaceAll("_", " ")}
                    </Pill>
                    {admission.checked_at ? <span className="mt-1 block">Checked {new Date(admission.checked_at * 1000).toLocaleString()}</span> : null}
                    {admission.status === "review_required" ? <p className="mt-1">Retained for review, but blocked from build prompts and automatic activation.</p> : null}
                    {admission.blocking_findings?.length ? <p className="mt-1">{admission.blocking_findings.join(" ")}</p> : null}
                    {admission.status === "failed" ? <p className="mt-1 text-ember">The saved check could not be verified. This skill is not eligible.</p> : null}
                  </div>
                  {s.content_audit ? (
                    <details className="text-xs text-ash">
                      <summary className="cursor-pointer">
                        Rating and budget: {s.content_audit.advice_chars.toLocaleString()} chars ({s.content_audit.budget_percent}%)
                      </summary>
                      <ul className="mt-2 list-disc space-y-1 pl-4">
                        {s.content_audit.reasons.map((reason) => <li key={reason}>{reason}</li>)}
                      </ul>
                      <p className="mt-2">Budget: {s.content_audit.global_budget_chars.toLocaleString()} characters shared across selected skills.</p>
                      <p className="mt-1 break-all font-mono text-[10px]">Body: {s.content_audit.body_sha256}</p>
                    </details>
                  ) : null}
                  <p className="text-xs text-ash">
                    Historical build association: {s.uses > 0 && Number.isFinite(Number(s.score)) ? `${Math.round(Number(s.score) * 100)}% across ${s.uses} uses` : "untried"}.
                    {" "}Not proof of this skill's contribution.
                  </p>
                  <div className="flex flex-wrap gap-1">
                    {(s.categories || []).map((category) => (
                      <button type="button" key={category} className="btn-ghost px-2 py-0.5 text-[10px]" onClick={() => chooseCategory(category)}>{category}</button>
                    ))}
                  </div>

                  {externalCandidate && quarantined && !s.promotion_ready ? (
                    <p className="text-xs text-ember">
                      {provenanceComplete
                        ? legacyMigrated
                          ? "Quarantined: provenance is complete, but the retained source receipt did not verify."
                          : "Quarantined: provenance is complete, but this candidate did not pass the library's promotion gate."
                        : "Quarantined: immutable GitHub provenance is incomplete, so this reference cannot be promoted."}
                    </p>
                  ) : null}

                  <div className="flex flex-wrap gap-2" aria-label={`Evaluation for ${skillName}`}>
                    <Pill tone={evaluationTone}>
                      {evaluationStatus === "passed" ? "Static checks passed" : evaluationStatus === "failed" ? "Static checks failed" : "Not evaluated / stale"}
                    </Pill>
                    <span className="text-xs text-ash">Effectiveness: {String(capability.effectiveness || "not_checked").replaceAll("_", " ")}</span>
                  </div>
                  <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
                    {quarantined ? <Pill tone="ember">quarantined</Pill> : s.active ? <Pill tone="plasma">active</Pill> : <Pill tone="ember">not active</Pill>}
                    {externalCandidate && provenanceComplete ? (
                      <Pill tone="plasma">provenance complete</Pill>
                    ) : null}
                    {(s.topics || []).map((t) => (
                      <button key={t} type="button" className="btn-ghost px-2 py-0.5 text-[10px]" onClick={() => updateFilter("topic", t)}>
                        {t}
                      </button>
                    ))}
                  </div>

                  <SkillLifecyclePanel
                    slug={s.slug || s.name || s.id}
                    skillName={skillName}
                    capabilityStatus={s.capability_status}
                    expanded={expandedSkill === (s.slug || s.name || s.id)}
                    onToggle={() => toggleSkillDetail(s.slug || s.name || s.id)}
                    evaluations={evaluations}
                    evaluationsLoading={evaluationsLoading}
                    evaluationsError={evaluationsError}
                    evaluate={evaluateSkill}
                    activate={activateSkill}
                    rollback={rollbackSkill}
                    activationReason={activationReason}
                    setActivationReason={setActivationReason}
                    rollbackReason={rollbackReason}
                    setRollbackReason={setRollbackReason}
                  />

                  {s.promotion_ready ? (
                    <button
                      type="button"
                      className="btn-ember mt-1 self-start"
                      disabled={promoteExternal.isPending || !s.slug}
                      aria-label={`Promote ${skillName} after evidence review`}
                      onClick={() => {
                        setPromotionResult(null);
                        promoteExternal.mutate(s.slug);
                      }}
                    >
                      {promoteExternal.isPending ? "Reviewing evidence…" : "Promote reviewed skill"}
                    </button>
                  ) : null}
                </div>
              );
            })}
          </div>
        )}
        {browseMode === "skills" && matchingSkills.length > visibleCount ? (
          <div className="border-t border-hairline p-4">
            <button type="button" className="btn-ghost" onClick={() => setVisibleCount((count) => count + 36)}>
              Show more ({visibleCount} of {matchingSkills.length})
            </button>
          </div>
        ) : null}
      </Panel>

      <Panel className="mt-6">
        <PanelHead
          label="Build-pattern reuse"
          right={<span className="font-mono text-[11px] text-ash">/skills · patterns</span>}
        />
        {patterns.length === 0 ? (
          <Empty icon="◇">
            No build patterns recorded yet. Successful builds fill this scoreboard and promote repeat winners into skills.
          </Empty>
        ) : (
          <div className="overflow-auto">
            <table className="w-full text-left font-mono text-xs">
              <thead>
                <tr className="border-b border-hairline">
                  <th className="eyebrow px-4 py-3 font-normal">Stack</th>
                  <th className="eyebrow px-4 py-3 font-normal">Uses</th>
                  <th className="eyebrow px-4 py-3 font-normal">Win rate</th>
                  <th className="eyebrow px-4 py-3 font-normal">Mean score</th>
                  <th className="eyebrow px-4 py-3 font-normal">Shape</th>
                </tr>
              </thead>
              <tbody>
                {patterns.slice(0, 20).map((p, i) => (
                  <tr key={p.fp || `${p.stack || "pattern"}-${i}`} className="border-b border-hairline/60">
                    <td className="px-4 py-2 text-bone">{p.stack || "generic"}</td>
                    <td className="px-4 py-2 text-ash">{p.uses ?? 0}</td>
                    <td className="px-4 py-2 text-plasma">
                      {Math.round((p.win_rate ?? 0) * 100)}%
                    </td>
                    <td className="px-4 py-2 text-ash">
                      {Number(p.mean_score ?? 0).toFixed(1)}
                    </td>
                    <td className="max-w-xl truncate px-4 py-2 text-ash/80">
                      {JSON.stringify(p.shape || {})}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
