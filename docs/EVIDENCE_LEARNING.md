# Evidence-backed learning

SkyN3t’s learning features are deliberately split into three distinct steps:

1. Record a build decision and its outcome.
2. Evaluate a small configuration candidate against already-produced Golden evidence.
3. Let an operator explicitly promote only an externally derived skill that has immutable provenance.

Nothing in this flow runs a build, changes a prompt, changes routing, imports a
remote skill, or executes remote instructions merely because an evaluation passes.

## Build contract

Every Studio build records `extra.build_contract` in its manifest and includes
the identical mapping in `BUILD_STARTED` and stage extras. Schema version 1
freezes the selector decision, build classification, versioned layout profile,
build profile, and a stable SHA-256 digest.

The `template` entry is intentionally `{ "id": "", "version": 0,
"source": "none" }`: SkyN3t currently generates from the brief and its stage
agents, rather than selecting from a template catalog. The contract is evidence
of the actual build decision, not an invented catalog claim.

## Local human design feedback

A review note submitted for a delivered project can become a short, reusable
**advisory** design lesson. The Projects page sends the note to
`POST /api/projects/{slug}/feedback`; the endpoint accepts the review text and
an optional design category.

SkyN3t stores only bounded, distilled guidance in the shared `human_design`
lesson scope. The original comment is not treated as executable instructions,
and it cannot change settings, invoke tools, or publish a project. During a
later UI/design build, the runner retrieves matching human-design lessons
alongside same-stack lessons. A delivered build gives used lessons positive,
quality-weighted credit. A failed build records neutral exposure unless later
verifier evidence can attribute a specific conflict, avoiding collective blame
for every piece of advisory guidance in the prompt.

This is intentionally local and opt-in: feedback is only captured when a user
submits it. It is separate from remote GitHub/RAG material and does not train or
modify the underlying model.

## Evidence-only configuration evaluation

Use two completed, compatible Golden ledgers and a narrow JSON candidate:

```powershell
skyn3t cortex evaluate `
  --kind prompt `
  --candidate .\candidate.json `
  --baseline-ledger .\baseline.json `
  --candidate-ledger .\candidate-ledger.json
```

Accepted candidate kinds are `prompt`, `skill_policy`, and `router_policy`.
They accept only fixed configuration fields such as a bounded prompt template,
skill-selection thresholds/tags, or allowed local-routing preferences. Candidate
fields and text that carry code, commands, paths, URLs, credentials, or secrets
are rejected.

The command only reads the supplied ledgers. It saves a content-addressed record
under `<data_dir>/cortex/evaluations/`, including candidate and ledger hashes,
baseline revision, comparison summary, and a tamper-evident manifest hash.

- A passing Golden comparison becomes `review_required`.
- A failed, incomplete, incompatible, or unreadable comparison becomes `rejected`.
- Both `applied` and `promoted` are permanently `false` in this record type.

List verified records with:

```powershell
skyn3t cortex evaluations
```

This is intentionally separate from `skyn3t cortex ratchet`, which is an
opt-in before/after tuning experiment that runs real builds.

## GitHub-derived skills

GitHub documentation ingestion is read-only. SkyN3t keeps the README as the
repository-level RAG record and, only when GitHub supplies a full immutable
commit SHA, may fetch up to 24 small `*.md` files (README included) at that
exact revision. Each accepted Markdown document gets its own unreviewed RAG
record and source path; a failed extra document never fails the README ingest.

SkyN3t does not inject remote GitHub text directly into build prompts. New
GitHub RAG records carry `external_unreviewed`; older GitHub source URLs are
also excluded from automatic recall.

A substantive document can produce one advisory skill candidate. Every such
candidate is written as:

- `source: github-distilled`
- `external-candidate` and `hygiene:quarantine` tags
- provenance for canonical GitHub URL, that document's relative path,
  retained-content SHA-256, and, only when GitHub returned it, an immutable
  commit SHA and license

README keeps the repository's stable skill slug. Other Markdown documents use
separate path-derived slugs, so no guide can overwrite a README candidate or a
similarly named guide in another directory. Thin or non-actionable documents
are reported as skipped instead of creating placeholder skills.

Quarantined skills cannot be selected for normal build advice. A local operator
can make an eligible one advisory with:

```powershell
skyn3t cortex promote-skill <skill-slug>
```

Promotion requires a canonical `https://github.com/<owner>/<repo>` source, a
full immutable Git SHA, a SHA-256 evidence hash, and a source path. A branch
name is never treated as a pin. Promotion removes the quarantine/candidate tags
and adds `external-promoted`; the skill remains non-binding advice rather than
an executable instruction.

## Local agent catalogs

Local agent catalogs are also evidence-bound: import creates quarantined
`catalog-candidate` skills with an advisory-body hash and source-path receipt.
An API caller must pass `activate: true` to validate and promote that exact
local content to `catalog-promoted`. See [Swarm and skills](SWARM_SKILLS.md)
for the runtime role and replay behavior.

## Meaningful build-pattern evidence

Pattern reuse now fingerprints the meaningful build shape: ordered stage names,
agent types, declared capabilities, optional/gated roles, test-first choice,
and bounded best-of-N setting. It deliberately excludes the brief, file names,
and free-form notes. That lets the scoreboard distinguish a real test-first
pipeline from a superficially similar pipeline without retaining project text.

## Lab autonomy and Cortex triage

With the personal-lab autonomy profile enabled, bounded Repo Scout proposals
that identify a canonical GitHub repository can proceed without a repetitive
human decision. Cortex records the Lab-specific reason in the proposal audit
trail. This affects only the research/triage action: fetched source material is
still marked `external_unreviewed`, and any resulting skill remains a
quarantined candidate. Non-GitHub, malformed, deployment, credential, runtime,
and other high-impact actions retain their normal gates.

## Curated local skill hubs

Set `SKYN3T_SKILLS_HUB_PATHS` to one or more comma-separated **local** skill
folders. SkyN3t loads those Markdown files during normal CLI and web startup,
after seed skills, without executing hub scripts. Every accepted file is
namespaced, byte-hashed, retained below the local skill library, and passed
through skill hygiene. A missing, unsupported, or symlinked hub is skipped and
reported rather than silently becoming build guidance.

```powershell
$env:SKYN3T_SKILLS_HUB_PATHS = 'D:\Shared\skills,D:\Team\reviewed-skills'
skyn3t cortex skill-hubs
```

The command shows the last per-path import report, including active,
quarantined, skipped, and reason counts. An explicitly configured local folder
is the trust boundary; remote repositories are not fetched by this loader.

## Safe legacy-skill migration

Older `github-distilled` records that lack a complete immutable receipt are
never bulk-enabled. Curate one record at a time with a local copy of the exact
reviewed source evidence. The first command is a dry run; it changes nothing:

```powershell
skyn3t cortex migrate-legacy-skill <legacy-slug> `
  --source-url https://github.com/owner/repository `
  --revision <full-40-or-64-character-git-sha> `
  --source-path README.md `
  --evidence .\reviewed-source.md
```

After checking the displayed hash and path, rerun the same command with
`--apply`. SkyN3t retains the evidence bytes, creates a new quarantined
successor, and leaves the original legacy record inert. A receipt is rechecked
at promotion time; a missing or altered receipt cannot be promoted. Repeating
an identical `--apply` safely repairs only that still-quarantined matching
receipt; a mismatch or already-promoted record is refused. Promotion remains a
separate, one-skill human action:

```powershell
skyn3t cortex promote-skill <new-candidate-slug>
```

## Retiring or shelving skills

Retirement is separate from quarantine. A quarantined candidate remains in the
library for evidence review; retired material and reference-only documents are
kept outside the injectable library, with their original content and usage
receipts preserved for recovery.

The optional `.skill_retirements.json` file in the skills directory records
explicit exclusions:

```json
{
  "schema_version": 1,
  "skills": {
    "retired-example": {
      "disposition": "retired",
      "archive_path": "data/skill-maintenance/review/retired/retired-example.md",
      "body_sha256": "<sha256-of-the-original-advisory-body>"
    }
  }
}
```

`reference-only` is the other supported disposition. Archive paths are receipts,
not instructions to read, execute, or delete a file. The registry itself does
not move files; an operator must retain the archived content before removing its
live copy.

The library enforces these exclusions during loading, direct addition,
directory and configured-hub imports, legacy candidate creation, seed creation,
and pattern/external/catalog promotion. Thus an
archived seed cannot reappear at the next startup. A malformed registry is an
explicit initialization error, not permission to load excluded advice.
Without a registry, normal library behavior is unchanged.

Restore a record only after reviewing its current suitability: preserve a copy
of the current library, remove that record's exclusion, restore the selected
archived file, and reload the library. Existing external-evidence and promotion
requirements still apply. Keep current usage scores when restoring individual
records; older score snapshots are historical evidence, not replacements for
subsequent learning.

Winning-pattern statistics alone are not a reusable skill. Automatic promotion
still requires the existing usage/success thresholds, but empty or statistical
shapes such as `{"stages": 11}` do not produce instructional records.

Keyword and semantic selection use the same advisory renderer for the real
skill library. A semantic match therefore retains the procedure's prerequisites,
failure handling, and completion criteria instead of cutting them off at an
arbitrary 400-character boundary.

## Versioned reviewed snapshot

The Git checkout carries the reviewed skill Markdown files, retirement registry,
receipt indexes, and redistributable source/license evidence listed in
`data/skills/.distribution.json`. The manifest is a publication/integrity
inventory, not another importer or an activation mechanism. The normal library
loader still reads the configured skills directory.

The ignore rule remains in place for new runtime ingests, `.skill_scores.json`,
local hub state, and maintenance backups. Reviewed files are explicitly tracked;
publishing a new snapshot requires updating its inventory and deliberately
staging the reviewed paths rather than force-adding the whole data directory.
Normal usage grading updates the ignored score sidecar, not committed Markdown.

Receipt indexes may describe a source observed during local review whose raw
bytes are not redistributed. Every such omitted evidence path is listed under
`local_only_evidence` with its hash and reason. A source URL/hash is not a claim
that its raw content is bundled or that its license is known. Activated
`github-distilled` procedures normally retain their primary byte evidence in the
published snapshot. The explicit publication exception is an upstream document
with a webhook-shaped example blocked by push protection: its raw bytes remain
local, with the original URL/hash and curated advice unchanged. Protection is
not bypassed, and a redacted file is not mislabeled as the original source.
Unresolved candidates remain held. Third-party source/license/notice
files retain their own terms rather than inheriting the repository's MIT license.

Run the existing `tests/test_reviewed_skill_snapshot.py` tests after a snapshot
change. They copy only the publication inventory into an empty directory, so an
operator's untracked evidence cache cannot conceal missing published files.
This snapshot does not silently migrate or overwrite another configured data
directory, and it is not a new wheel-installation data migration.

## Deliberate limits

This first phase does not automatically mutate runtime settings, write code,
install a third-party agent framework, grant MCP tools new permissions, or
publish a change. It provides durable evidence and narrow promotion boundaries
so later learning work has a reproducible, reviewable foundation.
