# SkyN3t skill inventory audit - 2026-09-08

## Scope and evidence

This audit covers all **421 registered skill records**, including **82 active**
and **339 quarantined** records. Every local body received a content-usefulness
review. Source checks were attempted for all **394 external imports**; the
remaining **27 authored, seeded, or build-generated records** have no external
upstream to update against.

The companion [CSV](2026-09-08-skill-audit.csv) contains one sortable row per
skill. The [JSON](2026-09-08-skill-audit.json) retains the individual rationale,
content flags, source observations, revisions, local body hashes, and additional
review notes. [New-skill discovery](2026-09-08-skill-discovery.md) is a separate,
primary-source-backed shortlist, not an exhaustive ecosystem census.

No skill was installed, updated, deleted, promoted, or unquarantined. Audit
ratings do **not** replace the runtime `.skill_scores.json` usage scores.

## Freshness findings

| Population | Count | Observation |
|---|---:|---|
| Imported agent documents with changed upstream bodies | 20 | **16 worthwhile material updates**, plus 4 minor or integration-sensitive changes |
| Imported agent documents matching upstream | 7 | Normalized full imported body matches the current source |
| GitHub references whose retained excerpt still matches | 307 | Only the retained excerpt matches; this does not prove the whole project is current |
| GitHub references whose retained excerpt no longer matches | 29 | Source drift requiring review, not automatically obsolete technical advice |
| Curated summaries with reachable cited sources | 29 | Core advice reviewed: 21 supported, 5 partially supported, 2 citation mismatches, 1 version-context gap |
| GitHub reference with an insufficient excerpt | 1 | `gh-adamjanes-udemy-d3`; too little retained text for a useful freshness conclusion |
| GitHub reference with an unavailable source | 1 | `gh-astraadev-discord-all-tools-in-one`; repository lookup failed through GraphQL and REST |
| Local/generated skills | 27 | Content rated; external source freshness is not applicable |
| **Total** | **421** | Every inventory record accounted for |

There are **49 detected source differences**, but it would be misleading to call
all 49 obsolete skills: 20 are full imported documents and 29 are short reference
excerpts. Even an unchanged excerpt may be a badge, marketing text, or obsolete
project description.

**18 of the 338 GitHub-reference repositories are archived.** This overlaps the
excerpt counts rather than adding another population. An unchanged archived
README is not evidence that a project is maintained.

### The 339 quarantined records

All 338 `github-distilled` records were checked against their named upstream
repositories, including alternate README filenames. The remaining quarantined
record, `examples`, matches the current `idea-refine` support document but is not
a standalone build procedure. Consequently, the quarantine subset consists of
**308 matching retained texts, 29 changed excerpts, 1 insufficient excerpt, and
1 unavailable source**.

The quarantine is warranted as a content boundary, not a claim that every source
is malicious or outdated. The old import retained repository README snippets as
skills; matching current upstream text does not turn these snippets into useful
instructions. The library's
[hygiene rules](../../skyn3t/intelligence/skill_hygiene.py) and
[documented quarantine boundary](../SWARM_SKILLS.md#narrow-legacy-github-quarantine)
explain why source review must remain separate from activation.

## Material updates worth prioritizing

The 27 imported agent documents were compared with
[`addyosmani/agent-skills` at `6ca0cd7db39b41b1c37e26d335c507ee92382c6d`](https://github.com/addyosmani/agent-skills/tree/6ca0cd7db39b41b1c37e26d335c507ee92382c6d).
The following 16 active skills contain useful upstream improvements missing
locally. These are update recommendations, not assertions that every old
instruction is now wrong.

| Skill | Concrete missing improvement |
|---|---|
| `api-and-interface-design` | Idempotency-key handling, payload guards, and duplicate-request handling |
| `code-review-and-quality` | Structural remedies and a dependency-upgrade review workflow |
| `context-engineering` | Context-budget management and prioritization |
| `debugging-and-error-recovery` | Repository-specific verification commands instead of npm assumptions |
| `deprecation-and-migration` | Database schema expand/backfill/contract migrations |
| `documentation-and-adrs` | Inspect existing ADR conventions before introducing a new scheme |
| `git-workflow-and-versioning` | Release/versioning discipline and tag/changelog guidance |
| `incremental-implementation` | Stack-appropriate verification; new support-document link needs adaptation |
| `observability-and-instrumentation` | Entry-point attribution and concise operational runbooks |
| `performance-optimization` | Query-plan indexing, connection-pool diagnostics, caching, and keep/revert evidence |
| `planning-and-task-breakdown` | Stable plan destinations and avoiding overwriting incomplete plans |
| `security-and-hardening` | Broader path, dependency-installation, distributed-rate-limit, and privacy guidance |
| `shipping-and-launch` | Error-budget-based release gates |
| `source-driven-development` | Treat fetched documentation as untrusted data rather than new instructions |
| `spec-driven-development` | Decompose multi-capability requests before writing module specifications |
| `test-driven-development` | Discover the actual repository stack before choosing test commands |

Four additional source changes are not equivalent material upgrades:
`doubt-driven-development` and `frontend-ui-engineering` have wording/path
adjustments; `idea-refine` changes a script path; `using-agent-skills` introduces
references that require a companion skill/file not currently imported. Blindly
copying the latter would create another unresolved reference.

### Curated advice: citation problems are not automatically obsolete advice

All 29 curated summaries received a claim-level comparison with their current
cited source, in addition to the source-accessibility check. The JSON embeds
each comparison as `citation_review`; the CSV includes its verdict.

**21 have supported core advice, 5 have only partial citation support, 2 cite
different tooling than they recommend, and 1 needs framework-version context.**
Some supported core recommendations contain additional details not demonstrated
by that particular citation; those are retained explicitly in the review.

The two citation mismatches are
`consolidate-quality-gates-on-ruff-mypy-pytest` (the cited template configures
Black/isort/flake8 rather than Ruff) and
`two-tier-client-server-layout-with-dev-proxy-and-unified-prod-serving` (the
cited boilerplate is Webpack/CRA-based, not the Vite setup described by the
skill). These recommendations may still be useful, but their evidence needs
repair.

`centralized-error-handling-with-apierror-catchasync-wrapper` needs explicit
Express-version scope. Express 5 forwards rejected **returned promises**
automatically; unreturned promises and callback APIs still need explicit error
handling. A wrapper is not universally required for modern async handlers
([official Express error handling](https://expressjs.com/en/guide/error-handling/)).

Partial citation support affects accessible-form details, uv workflow details,
some Vite/React performance specifics, `.env` conventions, and API-client
implementation specifics. Missing citation support is not proof that those
details are wrong. In particular, a tutorial's use of MUI components does not
contradict semantic HTML; it simply does not teach all the accessibility checks
attributed to it.

## Rating method

Ratings measure the usefulness of the **installed local body to SkyN3t**, not
GitHub popularity, source-project quality, version currency, or benchmarked
performance. Every assigned body was read; separate review partitions used the
same scale and produced one rationale per record.

| Rating | Meaning |
|---|---|
| 5/5 | Excellent scoped procedure with explicit verification and failure handling |
| 4/5 | Strong actionable reusable procedure, with minor gaps |
| 3/5 | Useful advice, but incomplete, overly specific, or missing validation |
| 2/5 | Limited context requiring redistillation before reuse |
| 1/5 | No reusable build instruction, or unsuitable as a standalone factory skill |

Usefulness and trust are independent. A 5/5 rating is not an activation approval;
a famous repository can still produce a 1/5 imported marketing snippet. Ratings
are editorial judgments, not performance measurements.

| Usefulness | Active | Quarantined | Total |
|---|---:|---:|---:|
| 5/5 | 22 | 0 | 22 |
| 4/5 | 41 | 2 | 43 |
| 3/5 | 11 | 44 | 55 |
| 2/5 | 3 | 92 | 95 |
| 1/5 | 5 | 201 | 206 |
| **Total** | **82** | **339** | **421** |

Overall dispositions are **65 retain, 64 refine, 82 reference-only, and 210
retire**. "Retain" includes worthwhile skills with recommended upstream updates;
it does not mean their installed text should remain unchanged indefinitely.
Highest-rated short references and summaries were calibrated against the same
procedure/verification standard rather than accepting installation fragments as
excellent skills.

### Quarantined content is mostly a curation problem

Of the **339 quarantined records**, **293 rate only 1/5 or 2/5**, **44 rate
3/5**, and **2 rate 4/5**. The individual recommendations identify **205
GitHub-reference entries for retirement from the skill library**, rather than
spending time refreshing their README snippets. Nothing has been retired yet.

Of the **29 changed README excerpts**, **22 rate 1/5, 6 rate 2/5, and only 1
rates 3/5**. Therefore, automatically updating all changed references would
mostly refresh poor skill content. Re-curation or retirement is the more useful
decision.

`gh-visini-abstracting-fastapi-services` and `gh-tretapey-svelte-pwa` are the two
4/5 quarantined records because they preserve coherent setup/run procedures with
a test target or observable result. They still need provenance-backed curation
and stack/version review before promotion; a relatively complete old starter
recipe is not a recommendation to adopt its framework choices today.

## Worthwhile additions

The [discovery report](2026-09-08-skill-discovery.md) identifies **nine**
non-identical additions or scoped merges, with source paths, research revisions,
license observations, and integration caveats.

| Priority additions | Why they add more than another generic prompt |
|---|---|
| Anthropic `mcp-builder` | MCP implementation and tool-use evaluation guidance, while preserving the local delivery contract |
| Supabase `supabase-postgres-best-practices` | Dedicated PostgreSQL query-plan, pooling, schema, and locking advice |
| Expo `expo-native-ui` | Native controls, safe areas, platform-aware UI, and SDK-specific guidance |

Three conditional additions are Microsoft `playwright-cli`, Expo
`expo-data-fetching`, and Next.js `next-dev-loop`; each requires a matching
stack/tool runtime. The remaining candidates are **merges**, not three new
generic capabilities: Cloudflare web-performance trace advice, a pinned subset
of Vercel interface-review rules, and Addy Osmani's constraint-weakening review
guidance.

Do not import the Vercel mutable-fetch wrapper as if its rules were pinned, or
the constraint skill's bundled runner as proven enforcement. Do not recommend the
old `next-best-practices` skill path: upstream moved the workflow into the Next.js
repository. Full evidence and caveats are in the discovery report.

## Import-format problems to address

The **five active `pattern-*` auto-promoted records rate 1/5**. They record a
win percentage/build count and an identical stage count, but give no reusable
stack-specific instruction. These account for the five active retirement
recommendations; the other 205 are quarantined GitHub references.

`won-react-shape` embeds a canvas-game example under a general React label;
`won-python-shape` carries project-specific operational flags into generic CLI
advice. Both need scope/content refinement, not an upstream package update.

The `examples`, `frameworks`, and `refinement-criteria` records are support
documents belonging under `idea-refine`, not independent procedures. Interactive
interview/ideation instructions also need deliberate scoping before they can
serve an unattended build stage.

Several long procedures refer to `references/*-checklist.md`, sibling skills,
scripts, or external tools. SkyN3t's flat Markdown import does not automatically
resolve those dependencies or execute the scripts. Updating a main `SKILL.md`
without preserving/adapting its required context can leave the import incomplete.
There is also substantive overlap between the browser-testing section embedded
in `test-driven-development` and `browser-testing-with-devtools`.

## Interpretation limits

The original imported records contain no source revision/hash receipts.
Recovered origins and today's pinned observations are audit evidence, not
retroactive approval of historical imports.

README checks compare the entire retained excerpt after whitespace
normalization, not the whole repository. Full agent-document checks compare the
parsed body, including description handling consistent with the importer.
Curated summaries are paraphrases rather than copied documents: without an
original revision, only direct claim-level review can establish whether a
particular recommendation still holds.

Repository archival, missing license identification, broken source links,
content usefulness, and source freshness are distinct dimensions. None is
silently substituted for another in the per-record dataset. An unavailable
repository is not claimed to be permanently deleted.
