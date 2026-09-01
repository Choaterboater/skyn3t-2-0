# Phase B — Self-Learning That Makes Apps Better (design)

_SkyN3t 2.0 · 2026-06-20 · grounded by the `skyn3t-learning-status-v2` verification workflow._

## Goal

Close the captured-but-unconsumed learning loops so the factory's **shipped apps**
improve over time — not just its internal knobs. Phase A built the cockpit and
emits `STAGE_DEBUG_ATTEMPT` events (with `score_before`/`score_after`); Phase B
**consumes** them.

## Verified current state (what's wired vs not)

| Seam | Status | Note |
| --- | --- | --- |
| Reward grading of skills/lessons | **binary** | graded only by `verdict=='go'`; the debug score-deltas are emitted but **unconsumed** |
| `Reflector.propose_prompt_improvement` | **unwired** | only called in tests; no transcript collection; agent instructions are hardcoded module constants |
| GitHub→RAG→skill distill | **implemented, bounded per-file** | README stays the repo record; up to 24 commit-pinned `*.md` documents receive isolated unreviewed RAG records and quarantined skill candidates. |
| `ModelTournament.record_win` | partial (debate-only, gated off) | low value — deferred |
| `PromptEvolver` | unwired/orphaned | grounding says **use Reflector instead, delete PromptEvolver** — deferred |

## Slices (each its own commit, TDD, merged to main)

- **B1 — Continuous (score-delta) reward** *(this slice).* Grade advisory skills by the
  build's actual score (0–1), not binary go/no_go. Backward-compatible. Sharper skill
  ranking → better injection → better apps.
- **B2 — Reflector-driven learning proposals.** Collect per-stage transcripts; on
  `BUILD_COMPLETED` call `Reflector.propose_prompt_improvement` per agent and emit a
  `PROPOSAL_CREATED` (visible in the cortex). Makes the factory *visibly* learn what to
  fix — and directly addresses "the cortex doesn't add anything." (Observe-only; does
  not yet auto-apply.)
- **B3 — Per-file skills ingest.** ✅ Implemented: GitHub ingest lists and fetches up to
  24 bounded `*.md` documents only at a GitHub-supplied commit SHA, then distills one
  separately attributed, quarantined candidate per substantive file. README retains the
  repository-level RAG record; extra documents have isolated unreviewed RAG records.
## Deferred (explicit)

- **B-later — apply evolved instructions.** Wiring the evolved `base_instruction` back into
  the app-writing agents needs an instruction-injection refactor (agents use hardcoded
  `_SYSTEM` constants). Higher risk; its own slice after B2 proves the proposals are good.
- **PromptEvolver** (delete in favor of Reflector) and **ModelTournament** population —
  low value now.

## B1 design (implemented here)

- `Skill.quality_sum: float | None` — sum of per-use quality in [0,1]. Defaults (via
  `__post_init__`) to `float(helpful)` so skills graded before this existed keep their
  exact score (no regression).
- `Skill.score = quality_sum / uses` (was `helpful / uses`).
- `record_use(slugs, *, helpful, quality=None)` — `quality` gives the continuous reward;
  omitted → falls back to `1.0`/`0.0` (old binary behavior).
- `StudioRunner._record_learning` passes `quality = manifest.score / 100`.
- Persisted in the skill markdown front matter and round-tripped by `parse_skill`.

## Testing

Offline pytest: continuous grading distinguishes a 0.9 build from a 0.3 build; binary
path unchanged; an old skill (no `quality_sum`) keeps its score; markdown round-trips;
and `_record_learning` grades by the build's score end-to-end.
