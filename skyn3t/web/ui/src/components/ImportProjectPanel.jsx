import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { apiPost } from "../api.js";
import { importProjectBody } from "../projectImport.js";
import { Panel, PanelHead } from "./ui.jsx";

export default function ImportProjectPanel({ onImported, onClose }) {
  const qc = useQueryClient();
  const [path, setPath] = useState("");
  const [slug, setSlug] = useState("");
  const [stack, setStack] = useState("");
  const mutation = useMutation({
    mutationFn: (body) => apiPost("/projects/import", body),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      onImported(result);
    },
  });
  const result = mutation.data;
  const skipped = Array.isArray(result?.skipped) ? result.skipped : [];
  const warnings = Array.isArray(result?.warnings) ? result.warnings : [];
  const inputClass = "mt-1 w-full rounded border border-hairline bg-ink/60 px-3 py-2 font-mono text-xs text-bone placeholder:text-ash/50 focus:border-ember focus:outline-none";

  function submit(event) {
    event.preventDefault();
    if (mutation.isPending) return;
    mutation.mutate(importProjectBody({ path, slug, stack }));
  }

  return (
    <Panel className="mb-4">
      <PanelHead
        label="Import existing project"
        right={
          <button type="button" className="btn-ghost" onClick={onClose} disabled={mutation.isPending}>
            Close
          </button>
        }
      />
      <div className="space-y-4 p-4">
        <p className="text-sm text-ash">
          Work on a project built anywhere. Import creates a managed copy and leaves
          your original untouched. It does not install dependencies, execute code,
          or start an AI run.
        </p>
        <form onSubmit={submit}>
          <fieldset disabled={mutation.isPending} className="space-y-3 disabled:opacity-60">
            <label className="block text-xs text-bone">
              Local project directory
              <input
                value={path}
                onChange={(event) => setPath(event.target.value)}
                placeholder="/absolute/path/to/existing-project"
                className={inputClass}
                aria-describedby="import-path-help"
                autoComplete="off"
                required
              />
            </label>
            <p id="import-path-help" className="text-xs text-ash">
              Use an absolute path on the machine running SkyN3t, not a GitHub URL
              or a browser upload. For a monorepo, select the application folder.
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block text-xs text-bone">
                Managed name (optional)
                <input
                  value={slug}
                  onChange={(event) => setSlug(event.target.value)}
                  placeholder="defaults to the folder name"
                  pattern="[a-z0-9][a-z0-9-]{0,79}"
                  maxLength={80}
                  title="Lowercase letters, digits and hyphens; an existing name cannot be overwritten."
                  className={inputClass}
                />
              </label>
              <label className="block text-xs text-bone">
                Stack override (optional)
                <input
                  value={stack}
                  onChange={(event) => setStack(event.target.value)}
                  placeholder="automatic, or react / python / nextjs"
                  className={inputClass}
                />
              </label>
            </div>
            <p className="text-xs text-ash">
              Known credential files, Git metadata, dependencies, caches, build
              output and symlinks are excluded. This is not a secret-content scan;
              review the copy before using an AI provider.
            </p>
            <button type="submit" disabled={!path.trim() || mutation.isPending} className="btn-ember disabled:opacity-50">
              {mutation.isPending ? "Importing copy..." : "Import copy"}
            </button>
          </fieldset>
        </form>
        {mutation.isError ? (
          <p role="alert" className="text-sm text-ember">
            Import failed: {String(mutation.error?.message || mutation.error)}
          </p>
        ) : null}
        {result ? (
          <div role="status" aria-live="polite" className="space-y-2 border-t border-hairline pt-3 text-xs text-ash">
            <p className="text-bone">
              Imported {result.slug}: {result.files_count} files, stack {result.stack}.
              The copy is unverified.
            </p>
            <p className="break-all font-mono">{result.project_dir}</p>
            <p>Use the Improve form below to describe a fix, refactor, feature, or redesign. No changes have been requested yet.</p>
            {warnings.map((warning, index) => <p key={index}>{warning}</p>)}
            {skipped.length ? (
              <details>
                <summary className="cursor-pointer text-bone">{skipped.length} excluded entries</summary>
                <ul className="mt-2 max-h-48 space-y-1 overflow-auto font-mono">
                  {skipped.slice(0, 100).map((item) => (
                    <li key={item.path} className="break-all">{item.path}: {item.reason}</li>
                  ))}
                </ul>
                {skipped.length > 100 ? <p>Showing the first 100 entries. The complete receipt is in skyn3t_manifest.json.</p> : null}
              </details>
            ) : null}
          </div>
        ) : null}
      </div>
    </Panel>
  );
}
