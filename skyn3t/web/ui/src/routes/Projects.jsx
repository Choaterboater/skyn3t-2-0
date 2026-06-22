import React, { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { queryFn, apiFetch, apiPost } from "../api.js";
import {
  PageHeader,
  Panel,
  PanelHead,
  Pill,
  Empty,
  verdictTone,
} from "../components/ui.jsx";

function fmtMB(bytes) {
  if (bytes == null) return "—";
  return `${(bytes / 1_048_576).toFixed(1)} MB`;
}

function fmtCost(usd) {
  return usd == null ? "—" : `$${Number(usd).toFixed(4)}`;
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
        label="Cleanup"
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
          Run a scan to see what can be cleaned up.
        </div>
      )}
    </Panel>
  );
}

export default function Projects() {
  const qc = useQueryClient();
  const [confirmSlug, setConfirmSlug] = useState(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["projects"],
    queryFn: queryFn("/projects"),
  });

  const del = useMutation({
    mutationFn: (slug) => apiFetch(`/projects/${slug}`, { method: "DELETE" }),
    onSuccess: () => {
      setConfirmSlug(null);
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const projects = Array.isArray(data) ? data : data?.projects || [];

  return (
    <div>
      <PageHeader
        eyebrow="Foundry · Project Vault"
        title="Projects"
        sub="All built artifacts. Preview, delete, or clean up stale outputs."
        actions={
          <span className="badge border-hairline text-ash">
            {projects.length} project{projects.length !== 1 ? "s" : ""}
          </span>
        }
      />

      {error ? (
        <Panel className="mb-6 border-ember/40 p-4 text-sm text-ember">
          Could not load projects: {String(error.message)}
        </Panel>
      ) : null}

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
                  <th className="px-4 py-2 font-normal">Slug</th>
                  <th className="px-4 py-2 font-normal">Stack</th>
                  <th className="px-4 py-2 font-normal">Status</th>
                  <th className="px-4 py-2 font-normal">Score</th>
                  <th className="px-4 py-2 font-normal">Cost</th>
                  <th className="px-4 py-2 font-normal">Size</th>
                  <th className="px-4 py-2 font-normal">Preview</th>
                  <th className="px-4 py-2 font-normal"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-hairline/60">
                {projects.map((p) => {
                  const isConfirming = confirmSlug === p.slug;
                  return (
                    <tr key={p.slug}>
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
                          <button
                            onClick={() => setConfirmSlug(p.slug)}
                            className="btn-ghost text-ember/70 hover:text-ember"
                          >
                            Delete
                          </button>
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
