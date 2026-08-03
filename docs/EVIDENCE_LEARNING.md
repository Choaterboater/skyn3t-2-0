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

GitHub README ingestion is read-only. Its text can be stored as an unreviewed
RAG reference, but SkyN3t does not inject that remote text directly into build
prompts. New GitHub RAG records carry `external_unreviewed`; older GitHub source
URLs are also excluded from automatic recall.

When a substantive advisory skill is distilled, it is written as:

- `source: github-distilled`
- `external-candidate` and `hygiene:quarantine` tags
- provenance for canonical GitHub URL, README path, retained-content SHA-256,
  and, only when GitHub returned it, an immutable commit SHA and license

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
## Deliberate limits

This first phase does not automatically mutate runtime settings, write code,
install a third-party agent framework, grant MCP tools new permissions, or
publish a change. It provides durable evidence and narrow promotion boundaries
so later learning work has a reproducible, reviewable foundation.
