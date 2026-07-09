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

export default function Skills() {
  const queryClient = useQueryClient();
  const [catalogPath, setCatalogPath] = useState("");
  const [catalogResult, setCatalogResult] = useState(null);
  const [catalogError, setCatalogError] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["skills"],
    queryFn: queryFn("/skills"),
  });

  const skills = Array.isArray(data) ? data : data?.skills || [];
  const patterns = Array.isArray(data?.patterns) ? data.patterns : [];
  const summary = catalogResult?.summary || {};
  const catalogEntries = Array.isArray(catalogResult?.entries) ? catalogResult.entries : [];

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
        sub="Reusable capabilities the swarm has learned and registered in the hub."
        actions={
          <span className="badge border-hairline text-ash">
            learned · <span className="ml-1 text-plasma">{skills.length}</span>
          </span>
        }
      />

      {error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Skills hub unreachable: {String(error.message)}
        </Panel>
      ) : null}

      <div className="mb-6 grid grid-cols-2 gap-4 md:grid-cols-3 lg:grid-cols-4">
        <Stat label="Registered" value={skills.length} tone="plasma" hint="capabilities" />
        <Stat
          label="Tagged"
          value={skills.filter((s) => Array.isArray(s.tags) && s.tags.length).length}
          hint="carry tags"
        />
        <Stat
          label="Scored"
          value={skills.filter((s) => s.score != null).length}
          hint="have a score"
        />
        <Stat
          label="Patterns"
          value={patterns.length}
          tone={patterns.length ? "ember" : "bone"}
          hint="build shapes"
        />
        <Stat
          label="Catalog"
          value={summary.entries ?? 0}
          tone={summary.entries ? "plasma" : "bone"}
          hint="previewed roles"
        />
      </div>

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
            <SummaryChips title="Stacks" items={summary.by_stack} />
            <SummaryChips title="Stages" items={summary.by_stage} />
            <SummaryChips title="Risk" items={summary.by_risk} />
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
            {skills.map((s) => (
              <div
                key={s.name || s.id}
                className="panel flex flex-col gap-3 border-hairline bg-void/40 p-4 transition-all duration-300 hover:border-plasma/40"
              >
                <div className="flex items-start justify-between gap-2">
                  <h3 className="font-display text-base font-bold text-bone">
                    {s.name}
                  </h3>
                  {s.score != null ? (
                    <span className="font-mono text-sm text-plasma">{s.score}</span>
                  ) : null}
                </div>

                {s.description ? (
                  <p className="text-sm text-ash">{s.description}</p>
                ) : null}

                {Array.isArray(s.tags) && s.tags.length ? (
                  <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
                    {s.tags.map((t) => (
                      <Pill key={t} tone="plasma">
                        {t}
                      </Pill>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
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
