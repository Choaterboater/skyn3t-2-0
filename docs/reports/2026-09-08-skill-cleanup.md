# Skill cleanup and Hermes follow-up - 2026-09-08

## Applied outcome

SkyN3t's running service at `http://127.0.0.1:6660` now uses the reviewed library.
The primary cleanup was applied at 19:16 UTC; the separate Hermes merge followed
at 19:32 UTC, preserving the requested order.

| State | Before | After |
| --- | ---: | ---: |
| Registered skills | 421 | 137 |
| Active skills | 82 | 100 |
| Quarantined skills | 339 | 37 |
| Ready for promotion | 0 | 0 |

**210 records were retired and 82 were moved to reference-only storage.** All
292 original files remain recoverable outside the injectable library. Nineteen
previously quarantined references were curated and activated; eight new
advisories were added. Nine previously active records were removed from active
use, so the active total increased by 18 rather than 27.

The [complete action ledger](2026-09-08-skill-cleanup.json) and
[spreadsheet-friendly CSV](2026-09-08-skill-cleanup.csv) account for every one of
the original 421 records plus all eight additions. The
[original audit](2026-09-08-skill-audit.md),
[original ratings CSV](2026-09-08-skill-audit.csv), and
[source-backed audit JSON](2026-09-08-skill-audit.json) remain unchanged as the
before snapshot. Their ratings describe the original bodies, not the rewritten
versions or measured performance of future builds.

## What changed

- Refreshed and compacted all 23 retained imported agent procedures, including
  the worthwhile upstream updates identified in the audit.
- Reworked 56 retained GitHub references: activated 19 with appropriate scope
  and evidence; kept 37 on explicit review holds.
- Corrected 12 curated/local procedures. Examples include Express 4 versus 5
  error forwarding and wildcard routing, Vite 8 chunk configuration and bounded
  stale-chunk recovery, explicit configuration failures, and the actual
  `agent_pack` catalog/layout/validation contract.
- Preserved the bodies of 21 other curated summaries while recording their
  source observations. Across the whole library, 91 original bodies changed,
  eight were added, and 38 retained bodies stayed unchanged.
- Enriched existing performance, frontend-interface, and code-review guidance;
  subsequently merged Hermes-derived response-semantics proof into the API
  procedure. Evaluator calibration is part of the new RAG advisory rather than
  another redundant standalone skill.

Parent review narrowed the initial reference-curation suggestions. It corrected
FastAPI/Node/SvelteKit/Tauri applicability, removed destructive repository-reset
recipes from active advice, and held material involving incompatible runtimes,
unreviewed global integrations, privileged tenant bootstrap, automatic database
migrations, or unstable package choices. Source matching alone was not treated
as evidence that a README's instructions are suitable for current factory work.

The Vite 8.2.2 evidence uses the resolved release **commit**
`de1111ab0be00879b404e7ed3b2a80e264edddc1`, not its annotated tag object's SHA.
Both the build guide and configuration definitions were retained.

## Eight additions

These are scoped advisory procedures, not newly installed executables, browsers,
model backends, simulators, or cloud services. Existing project tooling and
explicit prerequisites remain authoritative.

| New advisory | Editorial rating | Purpose / boundary |
| --- | ---: | --- |
| MCP implementation and evaluation | 5/5 | Real protocol discovery/calls, schemas, bounded tools, negative cases |
| PostgreSQL query and schema engineering | 5/5 | Plans, indexes, connection budgets and safe migrations; PostgreSQL only |
| Expo native interface | 4/5 | Platform controls, accessibility, navigation and actual target-runtime proof |
| Playwright browser workflow proof | 5/5 | Persistent assertions, isolated state, retained failure evidence |
| Expo native networking | 5/5 | Offline states, bounded retries, cancellation and protected storage |
| Next.js framework/browser proof | 4/5 | Conditional introspection with version checks and existing-tool fallback |
| RAG retrieval and grounding evaluation | 4/5 | Separate retrieval/grounding metrics, abstention and calibrated judging |
| Swift async state and test isolation | 4/5 | Awaited behavior, actor/resource isolation and honest UI-proof boundaries |

Ratings use the original audit's content-quality scale. They are editorial
assessments, not success percentages or replacements for learned runtime scores.
Exact sources, pins, licensing and per-addition rationale are in the action
ledger and the [discovery](2026-09-08-skill-discovery.md) /
[gap-additions](2026-09-08-skill-gap-additions.md) reports.

**Historical-source exception:** the RAG advisory adapts framework-independent
concepts from an explicitly MIT-licensed, archived `hamelsmu/evals-skills`
revision. No archived implementation or unlicensed successor was adopted.
Swift testing guidance does not provide complete native GUI
launch/window/input/quit/relaunch proof.

## Hermes follow-up

Reviewed eight official releases in the 30-day window. The latest release
observed was [Hermes Agent v0.21.1 / `v2026.9.7`](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.7),
published September 7 at 22:17:01 UTC, resolved to
`2237be355906fbe6065ce1815711eee52b2d646e`.

Applied **one merge and no additional standalone skill**:
`api-and-interface-design` now includes HTTP representation and payload checks,
GraphQL application errors despite HTTP 200, exact fixture assertions, bounded
diagnostics, and bodyless-response handling. The original API-design procedure
was retained. This guidance is newly adopted by SkyN3t; it was not newly
introduced by the latest Hermes release.

The source Markdown and MIT license were retained and the merged advisory went
through a staged candidate/promotion cycle. Nothing installed or upgraded the
Hermes runtime, its global settings, debug adapters, or computer-control tools.
Other inspected candidates duplicated existing guidance or required separate
runtime integration. See the [Hermes review](2026-09-08-hermes-update-review.md)
for exact release dates, source citations, decisions, and the limits of the
upstream comparison.

## Why 37 remain held

**37 is a hold count, not an outdated-skill count.** The ledger gives each
record's specific reason. These include missing or incomplete primary evidence,
license-receipt gaps, archived/legacy interfaces, unsupported factory runtimes,
and integrations that require a project-specific authorization or compatibility
decision.

An automated license lookup failure is not proof that a project is unlicensed.
For example, the `baugarten/node-restful` source contains an embedded MIT notice
despite the GitHub license endpoint returning 404; its recorded hold exposes
that discrepancy rather than claiming the notice is absent. Further review may
resolve a receipt or scope gap without changing the upstream project.

Held entries carry `review-held` and quarantine tags, not `external-candidate`.
None is currently promotion-ready. Fetching missing primary documentation,
reviewing a compatible runtime, or resolving license evidence is a separate
review step; blindly toggling status is not a substitute.

## Persistence and runtime safeguards

The optional `.skill_retirements.json` records all 292 exclusions. Loading,
seeding, direct addition, directory/hub imports, legacy candidate creation, and
promotion honor those exclusions. Invalid registry contents and symlink
registries fail closed. These are slug-based exclusions, not a blanket ban on
every future version of an upstream project.

Automatic pattern promotion now rejects empty/count-only shapes such as
`{"stages": 11}`. Historical pattern statistics remain on the pattern board as
telemetry; they are not automatically reusable advice.

Semantic selection now uses the same advisory renderer as ordinary selection.
Previously it could cut a procedure at 400 characters, omitting prerequisites,
failure handling and completion criteria even while reporting the skill as
selected.

All 129 retained original usage records were preserved. Source/body hashes and
118 per-skill evidence indexes were retained. Mutable website observations are
labeled as observations rather than invented immutable Git revisions.

## Observed evidence

The final live API returned exactly 137 records: 100 active, 37 quarantined,
zero promotion-ready. Every API payload matched the disk library's actual
serialization contract, and the service reported `stale_code: false`.

The real offline StudioRunner selection path exercised 78 cases, including all
eight additions and aliases, approved references, corrected local/curated
procedures, and the Hermes merge. Selected procedures retained their complete
bodies; unrelated-stack and quarantine exclusions held. Seeding recreated no
retired record. All 27 historical count-only patterns returned no promotion in
an empty library, independently of retirement exclusions.

Targeted regression coverage passed 91 tests for retirement, pattern quality,
imports, external/legacy promotion, rendering and learning integration. The
changed code also passed the existing Ruff checks. These observations establish
library integrity and selection behavior, not a benchmark of generated
applications or a claim that every upstream package was installed and tested.

## Recovery

The reviewed Markdown records, exclusion registry, receipt indexes and
redistributable evidence are included in the versioned snapshot described by
`data/skills/.distribution.json`. Private runtime sidecars, maintenance backups,
raw source content without established redistribution terms, and an upstream
webhook-shaped example blocked by push protection remain local and Git-ignored.
Omitted raw evidence is explicitly listed in the distribution
manifest; receipt metadata does not imply those bytes are present in a clone.

| Path | Purpose |
| --- | --- |
| `data/skills/` | Versioned reviewed snapshot plus ignored instance-local state and evidence |
| `data/skill-maintenance/2026-09-08-reviewed-cleanup/` | Original 421-record snapshots, retired/reference-only files, score receipts, action manifests, curation inputs and audit evidence |
| `data/skill-maintenance/2026-09-08-hermes-api-merge/` | Separate pre-Hermes snapshot, one-skill merge and apply receipts |

Both maintenance runs retain `before/` and `displaced-at-apply/` snapshots.
The original cleanup manifest is historical; `effective-manifest.json` records
the final expected library after the Hermes merge.

1. Stop only the owning, idle SkyN3t service before restoring files.
2. Preserve the current library and latest score sidecar first.
3. For an individual restoration, review the archived advice, remove only its
   deliberate exclusion, and restore the selected file with its required
   evidence. Preserve current scores for surviving skills; archived score
   snapshots must not overwrite later learning.
4. For a whole-library rollback, select the intended snapshot explicitly and
   reconcile any later records/scores before swapping directories. Keep the
   displaced current state until the restored service is working.
5. Restart from this checkout with
   `PYTHONPATH="$PWD" .venv/bin/skyn3t start --web --host 127.0.0.1 --port 6660`
   and inspect the resulting library state.

No upstream scripts or dependency installations were executed, and global
Copilot skill installations were not changed.
