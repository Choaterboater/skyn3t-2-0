import React, { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, apiPost, queryFn } from "../api.js";
import { PageHeader, Panel, PanelHead, Stat, Pill, Empty } from "../components/ui.jsx";

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
export default function Skills() {
  const queryClient = useQueryClient();
  const [catalogPath, setCatalogPath] = useState("");
  const [catalogResult, setCatalogResult] = useState(null);
  const [catalogError, setCatalogError] = useState("");
  const [promotionResult, setPromotionResult] = useState(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["skills"],
    queryFn: queryFn("/skills"),
  });

  const skills = Array.isArray(data) ? data : data?.skills || [];
  const patterns = Array.isArray(data?.patterns) ? data.patterns : [];
  const skillSummary = data?.summary || {};
  const catalogSummary = catalogResult?.summary || {};
  const catalogEntries = Array.isArray(catalogResult?.entries) ? catalogResult.entries : [];
  const quarantinedFallback = skills.filter(isQuarantined).length;
  const registeredCount = countOrFallback(skillSummary.registered, skills.length);
  const activeCount = countOrFallback(skillSummary.active, skills.length - quarantinedFallback);
  const quarantinedCount = countOrFallback(skillSummary.quarantined, quarantinedFallback);
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
        sub="Active skills can guide builds. Quarantined external references stay blocked until their immutable evidence is explicitly reviewed."
        actions={
          <span className="badge border-hairline text-ash">
            learned · <span className="ml-1 text-plasma">{registeredCount}</span>
          </span>
        }
      />

      {error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Skills hub unreachable: {String(error.message)}
        </Panel>
      ) : null}

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-3 xl:grid-cols-6">
        <Stat label="Registered" value={registeredCount} tone="plasma" hint="all records" />
        <Stat label="Active" value={activeCount} tone="plasma" hint="eligible to inject" />
        <Stat
          label="Quarantined"
          value={quarantinedCount}
          tone={quarantinedCount ? "ember" : "bone"}
          hint="blocked pending review"
        />
        <Stat
          label="Ready to promote"
          value={promotionReadyCount}
          tone={promotionReadyCount ? "ember" : "bone"}
          hint="passes promotion gate"
        />
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
          right={<span className="font-mono text-[11px] text-ash">/skills</span>}
        />
        {isLoading ? (
          <Empty icon="≋">Loading the library…</Empty>
        ) : skills.length === 0 ? (
          <Empty icon="◇">No skills registered. The swarm has nothing learned yet.</Empty>
        ) : (
          <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 lg:grid-cols-3">
            {skills.map((s) => {
              const tags = tagsFor(s);
              const quarantined = isQuarantined(s);
              const externalCandidate = tags.includes("external-candidate");
              const legacyMigrated = tags.includes("legacy-migrated");
              const provenanceComplete = s.provenance_complete === true;
              const skillName = s.name || s.title || s.slug || "Untitled skill";
              return (
                <div
                  key={s.slug || s.name || s.id}
                  className="panel flex flex-col gap-3 border-hairline bg-void/40 p-4 transition-all duration-300 hover:border-plasma/40"
                >
                  <div className="flex items-start justify-between gap-2">
                    <h3 className="font-display text-base font-bold text-bone">
                      {skillName}
                    </h3>
                    {s.score != null ? (
                      <span className="font-mono text-sm text-plasma">{s.score}</span>
                    ) : null}
                  </div>

                  {s.description ? (
                    <p className="text-sm text-ash">{s.description}</p>
                  ) : null}

                  {externalCandidate && quarantined && !s.promotion_ready ? (
                    <p className="text-xs text-ember">
                      {provenanceComplete
                        ? legacyMigrated
                          ? "Quarantined: provenance is complete, but the retained source receipt did not verify."
                          : "Quarantined: provenance is complete, but this candidate did not pass the library's promotion gate."
                        : "Quarantined: immutable GitHub provenance is incomplete, so this reference cannot be promoted."}
                    </p>
                  ) : null}

                  <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
                    {quarantined ? <Pill tone="ember">quarantined</Pill> : <Pill tone="plasma">active</Pill>}
                    {externalCandidate && provenanceComplete ? (
                      <Pill tone="plasma">provenance complete</Pill>
                    ) : null}
                    {tags.map((t) => (
                      <Pill key={t} tone="plasma">
                        {t}
                      </Pill>
                    ))}
                  </div>

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
