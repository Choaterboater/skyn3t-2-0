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

## Deliberate limits

This first phase does not automatically mutate runtime settings, write code,
install a third-party agent framework, grant MCP tools new permissions, or
publish a change. It provides durable evidence and narrow promotion boundaries
so later learning work has a reproducible, reviewable foundation.
