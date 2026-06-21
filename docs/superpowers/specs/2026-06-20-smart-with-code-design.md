# Smart with Code — SkyN3t Build Intelligence (Spec 1)

- **Date:** 2026-06-20
- **Status:** Design approved; implementation pending
- **Branch:** `feature/smart-with-code`
- **Author:** Stephen + Claude (paired)

## Motivation

A real build exposed three weaknesses at once. A teacher's brief — *"I want an
easy way to build a **web app** to build new daily lessons…"* — was stacked as
`python` and the explicit `stack:"fastapi"` pin was silently ignored. Root cause:

1. **Dropped stack hint.** `submit_build` passes the pin as `extra["stack"]`
   (`web/routes.py:134`), but `runner.start` reads `extra["stack_hint"]`
   (`studio/runner.py:650`). Different keys → every explicit pin is dropped and
   the planner free-chooses.
2. **No brain in stack selection.** `studio/planner.py:70-81` `detect_stack` is
   pure keyword matching, first-match-wins, ordered list, default `"python"`.
   It mis-stacks "web app" briefs and has no notion of *fit*.
3. **Skills don't reach web builds.** The two best design skills
   (`frontend-ui-engineering`, `api-and-interface-design`) are `stack:"generic"`
   with no tags, so `skill_library.relevant()` never auto-injects them for web
   builds.

Two independent research sweeps (a Substack survey and 22 AI-agent GitHub repos)
converged on the same gaps and surfaced one more cheap, high-ROI lever:

4. **No write-time validation.** `code_agent` writes files blind
   (`agents/code_agent.py` ~line 291 `target.write_text`), so syntax/import
   breakage only surfaces at proof/run — the most common (and most expensive)
   class of fix-loop iterations. SWE-agent (ACI), aider (`linter.py`), Cline, and
   Plandex all converge on edit-time validation as the single biggest performance
   lever — bigger than any model swap.

Notably, the MathPlan build (this same brief, scored 92) shows the agentic code
agent is strong enough to build a correct Flask web app *despite* the wrong
`python` stack label. So the stack label's real job is not "force the scaffold"
but to drive **(a)** proof/verification of the right thing, **(b)** the right
skill injection, and **(c)** the cockpit's framing.

## Scope

**In scope (Spec 1):**

1. Stack-hint plumbing fix + `--stack` CLI flag.
2. Intelligent stack selection (explicit pin → LLM best-fit → keyword fallback),
   restricted to the 6 stacks with real builders.
3. Marker-triggered conditional skill injection (so frontend/design skills fire
   for web/site stacks).
4. Edit-time lint/compile guardrail.
5. Project cleanup (failed / superseded / orphaned / stray-preview), trash +
   dry-run.

**Out of scope (future specs, listed so this spec stays coherent):**

- **Spec 2 — Measured self-improvement:** benchmark/regression harness gating
  self-rewrites; embedding-based skill+lesson retrieval; calibrated LLM-judge;
  per-stage cost attribution + "wasted tokens" cockpit panel.
- **Spec 3 — Operator mode:** "improve an already-built project" (`repo_map` →
  localize → diff-edit → test loop). `rag/repo_map.py` already exists.
- **Spec 4 — Fan-out orchestrator:** explore N divergent candidates in parallel,
  let `proof_run` referee, synthesize the delta. Gated by Spec 2's benchmark.

## Component 1 — Stack-hint plumbing fix

**Problem:** key mismatch (`extra["stack"]` written vs `extra["stack_hint"]`
read) drops explicit pins.

**Design:**

- `runner.start` reads `extra.get("stack") or extra.get("stack_hint")` as the
  canonical pin and threads it into `planner.plan(stack_hint=...)`. Keep both
  keys accepted for back-compat.
- Expose `--stack` on the `studio build` CLI (`cli/main.py:343`): add
  `stack: str = typer.Option("", "--stack")`, thread through `_run_build` →
  `start(extra={"stack": stack, ...})`.
- An unknown pin (not one of the 6 real builders) is ignored with a logged
  warning and falls through to selection — never a hard error.

## Component 2 — Intelligent stack selection

**Canonical menu** = the 6 stacks that have real builders
(`agents/_scaffold.py:_BUILDERS`): `react_vite`, `react_native`, `static_html`,
`python_cli`, `fastapi`, `node_express`. The planner-vocab `nextjs/flask/django`
are dropped as *named targets* — today they silently normalize to `react_vite`
anyway. Document the collapse in `_common._normalize_stack`.

**Selection order** (new `StackSelector`, injected into `Planner`):

1. **Explicit pin** (validated against the 6) → use it. `method="pin"`.
2. **LLM best-fit** (when an LLM client is available): prompt = brief + the menu
   with one-line "best for" descriptions; returns
   `{stack, confidence (0-1), rationale, clarifying_questions[]}`.
   `method="llm"`.
3. **Clarify on low confidence:** if `confidence < 0.6` **and** the build is
   *attended* → emit 2-3 targeted questions via `studio/clarification.py` before
   committing (gpt-engineer/Cline pattern). Default unattended → proceed with the
   LLM's best pick (keeps the factory autonomous).
4. **Keyword fallback** when no LLM: existing `detect_stack` logic, reordered so
   `react_native` sits **below** `fastapi`/`react` (fixes the `react_native`
   misfire). `method="keyword"`.
5. **Default** → `react_vite` (web-biased), not `python`.

**Recorded:** `manifest.extra["stack_selection"] = {method, stack, confidence,
rationale}` — feeds the learning loop and is shown in the cockpit.

**Interfaces:**

- `StackSelector.select(brief, *, pin="", llm=None, attended=False) -> StackChoice`
- `Planner.plan(...)` calls it; `detect_stack` stays pure/offline as the fallback
  path.

**Error handling:** any LLM error/timeout → fall back to keyword; never raise.
LLM result not in the menu → discarded, fall through.

## Component 3 — Marker-triggered skill injection

**Current:** `skill_library.relevant(stack, limit=5)` matches by stack-alias
group + tag intersection; generic design skills never match web builds.
`runner._skill_advice(stack)` at `runner.py:191`.

**Design:**

- Add optional `activation_conditions` to `Skill`
  (`{stacks[], file_markers[], brief_keywords[]}`). Default empty ⇒ current
  behavior (fully back-compat).
- `relevant(stack, *, markers=None, brief=None, limit)` makes a skill eligible
  when its stack matches the alias group **or** a marker/keyword condition fires;
  rank by `(condition_hits + stack_hit + tag_hit, score)`.
- For web/site stacks (`react_vite`, `fastapi`, `static_html`, `node_express`)
  the design skills are eligible via `activation_conditions.stacks`, so they
  auto-inject.
- Re-tag `data/skills/frontend-ui-engineering.md` and
  `data/skills/api-and-interface-design.md` frontmatter: add `tags` and
  `activation_conditions`.
- `runner._skill_advice` passes `brief` + `stack` (and, for the future
  improve-existing path, detected markers from `stack_detector`) so conditions
  can fire.

Embedding-based retrieval is **Spec 2**; Spec 1 keeps deterministic
marker/tag/stack matching.

## Component 4 — Edit-time lint/compile guardrail

**Current:** code is written blind; errors surface at proof/run.

**Design:**

- `validate_source(path, content) -> (ok, error)`:
  - `.py` → `compile(content, path, "exec")` (catches `SyntaxError`).
  - `.json` → `json.loads`; `.toml` → `tomllib.loads`.
  - `.js/.jsx/.ts/.tsx` → best-effort: `tsc --noEmit` on a temp file **if** a
    toolchain is available (gated, soft-skip offline); else a light
    brace/paren/bracket balance check. Cheap; never blocks on a missing
    toolchain.
- `_validate_then_write(agent, path, content)`: validate; on failure, feed the
  **exact** error back to the same agent for **one** bounded re-emit; if still
  failing, write anyway (best-effort — never lose generated work) and flag it so
  the downstream debug loop still catches it. Reuse the bounded-loop pattern from
  `studio/stage_debug.py`.
- Wrap the writes in `agents/code_agent.py` and `agents/code_improver.py`.

**Error handling:** validation is **advisory** — a validator that itself errors
(e.g. missing toolchain) is treated as "skip", not "fail". Content is never
dropped.

## Component 5 — Project cleanup

**Signals** (from codebase exploration):

| Category | Signal |
|---|---|
| failed | manifest/`BuildRow` `status` in `{failed, pending}`; or `no_go` with empty delivery |
| superseded | same `slug`, multiple builds — keep newest by `created_at` |
| orphaned worktree | dir in `~/Documents/.skyn3t_worktrees/` referenced by no live (`state.builds`) or persisted (`BuildRow.manifest.worktree_dir`) build |
| orphaned project | `Projects/<slug>` with no manifest **and** no `BuildRow` |
| stray `.preview/` | leftover `.preview` dir inside a delivered project (rejected rebuilds / live snapshots) |

**Design:**

- New `skyn3t/studio/cleanup.py`, pure functions:
  - `scan(state) -> CleanupReport` with one list per category; each item carries
    `{path, reason, size_bytes, last_modified}`.
  - `apply(report, *, trash_dir, dry_run=True, categories=...) -> CleanupResult`.
    **Moves** items to `~/Documents/.skyn3t_trash/<runtag>/` (recoverable) — never
    `rmtree` on a delivered dir. Git worktrees use `cleanup_worktree()`
    (`worktree.py:199`, `git worktree remove`) then move any residue.
- CLI: `skyn3t project cleanup [--apply] [--keep N] [--categories ...] [--yes]` —
  **dry-run by default**, prints a table of what *would* move + bytes reclaimed;
  `--apply` performs.
- API: `GET /api/projects/cleanup` (report only) and
  `POST /api/projects/cleanup {dry_run, categories}` (auth/loopback-gated),
  following the `web/routes.py` `build_router` pattern.
- **Auto:** after a successful delivery (`merge_back`), auto-trash that build's
  own now-orphaned worktree (`best_of_n` already removes non-winners; extend to
  always remove the winner's worktree post-merge). Failed/rejected builds are
  left for the manual sweep unless `SKYN3T_AUTO_CLEANUP=1` (default 0).
- **Safety:** `trash_dir` is excluded from scans; a running build's active
  worktree/project (`state.builds` + `status=="running"`) is never touched.

## Data flow

```
brief ──▶ submit_build(stack pin) ──▶ runner.start(extra.stack)
      ──▶ Planner.plan(StackSelector) ──▶ manifest.stack_selection
      ──▶ skill injection (marker-triggered, web⇒design skills)
      ──▶ code stage (_validate_then_write per file)
      ──▶ proof ──▶ deliver(merge_back ; auto-trash own worktree)

cleanup: scan(Projects/ + .skyn3t_worktrees/) ──▶ report ──▶ apply(trash, dry_run)
```

## Testing

Follow existing `tests/` patterns; full suite baseline 389 pass / 1 skip.

- **C1:** pin reaches the planner; `--stack` threads through the CLI; unknown pin
  ignored (not raised).
- **C2:** explicit pin wins; LLM-mock returns `{stack, confidence}`;
  low-confidence + unattended → proceeds with LLM pick; keyword fallback when no
  LLM; default is `react_vite` not `python`; rationale recorded in manifest.
- **C3:** web stack ⇒ design skills present; non-web ⇒ absent;
  `activation_conditions` fire on markers/keywords; skills with no conditions
  behave exactly as before.
- **C4:** broken `.py` is rejected + re-emitted; valid passes; missing toolchain
  → skip (not fail); generated content never lost.
- **C5:** fixture `Projects/` + worktrees → correct buckets; trash move is
  recoverable; dry-run is a no-op; a running build is untouched; git worktrees go
  through `cleanup_worktree`.

## Open decisions (resolved)

- Stack menu restricted to the **6 real builders** (`nextjs/flask/django` dropped
  as named targets — they already collapse to these).
- Cleanup = **trash (recoverable) + dry-run default**; no hard delete.
- Stack selection is **autonomous by default**; clarification only in attended
  mode at low confidence.

## Risks & mitigations

- **LLM stack-selector latency/cost per build** → cheap tier + cache by brief
  hash + keyword fallback. One extra short call per build.
- **`tsc` edit-time check slow for large TS** → gate on toolchain availability,
  validate only the file being written, soft-skip otherwise.
- **Cleanup removing something wanted** → trash (not delete) + dry-run default +
  never-touch-running + per-category opt-in.
