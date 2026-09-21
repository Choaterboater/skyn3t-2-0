# Swarm and skills

SkyN3t uses a runtime factory swarm to build a project, and it can also build
an `agent_pack` as a project artifact. Those are intentionally different
things.

## Runtime swarm versus generated agent packs

The runtime factory is assembled by `skyn3t.cli.main._assemble_spine()`.
It supplies the event bus, orchestrator, registered agents, intelligence
services, and `StudioRunner`. `StageSpec` definitions then route work through
the factory's plan, architecture, code, review, verification, and repair
paths.

`agent_pack` is instead a generated output stack. Its delivered product is
structured content such as Markdown personas and a `catalog.json` manifest.
It is treated as a content stack for delivery and proof. Generating one does
not install, start, or replace the factory's own runtime agents, and a generated
pack cannot change the roles that build the current project.

## A frozen build decision follows the swarm

Every Studio build persists its full Build Contract in the manifest. Before the
contract is handed to a worker, SkyN3t requires the exact supported schema,
an untruncated SHA-256 digest, and a successful recomputation check. It then
projects only compact, identifier-safe fields: the digest, schema version,
selected stack, app type, engine, layout profile, and build profile. Invalid,
tampered, or control-text-bearing contract data produces no handoff context.

When that verified projection is available:

- The Mixture-of-Agents council receives it as read-only plan context; advisors
  must not override it.
- Each normal stage task records the contract digest in its task metadata and
  final stage execution record.
- Each parallel code slice records the same digest in its slice scope, alongside
  its owned files. Slice results retain bounded ownership, model, and degraded
  outcome evidence for later inspection.

This keeps a council, an ordinary stage, and parallel worktrees tied to the
same decision while distinguishing verified contract fields from arbitrary
extra data.

## Skill selection and receipts

Skills are advisory guidance, not executable instructions or new tool
permissions. SkyN3t selects them in three complementary places:

- **Global advice** supplies stack-appropriate build guidance.
- **Stage-role advice** requires an explicit `stage:<name>` role match, so an
  architecture, code, review, or verifier role does not become generic advice
  for every stage.
- **Repair advice** first reuses the exact eligible code-stage role selection,
  then falls back to a repair-specific implementation/code/codegen role. This
  covers repair loops and one-off improve paths.

The verified contract can add `app_type:<value>` and `layout:<value>` ranking
tags. Those tags improve the rank of skills that declare them; they never make
an incompatible or quarantined skill eligible.

For each selected stage role, the build manifest records bounded evidence:

- `extra.skills_used` and `extra.stage_skills_used` identify selected advice.
  They do not alone prove complete prompt exposure: the global advice preamble
  currently has a 6,000-character limit and stage-role advice has a separate
  3,000-character limit. Inspect retained stage prompts to confirm what survived
  those limits.
- `extra.skill_selection_context` stores the compact verified contract context
  and the selection tags used for ranking.
- `extra.skill_selection_receipts[stage]` stores the selected slug, source,
  finite score at selection, tags, an `advisory_body_sha256` for every selected
  skill, and available provenance such as source URL, pinned revision, and
  source path. Skill bodies are not copied into the receipt.

At terminal outcome recording, the runtime stable-deduplicates the global and
stage selections and persists a manifest-scoped grading receipt before it
grades each selected skill. Re-entering terminal handling cannot double-grade a
role. The final build outcome sets its helpful signal and the final score
supplies a finite, bounded continuous quality value. A mid-stage result
therefore does not become the skill's final grade.

## Browsing and prompt-readiness ratings

The Skills dashboard opens with clickable categories instead of rendering the
entire library. Search narrows titles, descriptions, categories, and topics.
Selecting a category preserves the other filters and shows only matching skills;
topic chips narrow that selection further. Status, provisional rating, and prompt
size filters can be combined. Results are displayed 36 at a time without excluding
the remaining matches. Clear filters returns to the complete category index.

Each skill has a read-only, body-hashed **prompt-readiness** assessment. This
deterministic scan reads the complete body and flags reference extracts, unclear
scope/verification, weak build examples, and excessive rendered character cost.
Its 1–4 ratings are provisional text/budget judgments, not an independent content
review or a measured contribution to build quality; 5/5 is reserved for independent
review. The rating reasons and exact budget consumption are available on each card.
Historical build association and capability evidence remain separately labeled.
Browsing does not create checks or activate skills. The ingestion checks below
use concrete blocking findings, not the numerical rating, to control eligibility.

### Checks when skills arrive or change

All library write paths share an admission check: direct additions, directory and
curated imports, configured local hubs, migrated candidates, and learned build
patterns. The check runs synchronously when the skill is persisted, without a
dashboard visit or a later background tick. Loading a library also checks older
entries that have no current receipt.

The library stores the latest result under `.admission_checks/`, keyed by a hash
of the skill identity. Each receipt binds the complete content and applicability
metadata, includes a timestamp, categories/topics, and the provisional assessment,
and is replaced only when that assessment changes. Re-importing unchanged material
does not reset the timestamp or change historical usage scores. Character checks
reserve space for the longest normal score label rather than depending on its
current historical percentage and include the shared advisory heading.

Reference-extraction templates, weak claimed winning-build examples, empty
advice, and advice exceeding its complete budget (6,000 global characters or
3,000 for a stage-tagged role) are **review required**.
They remain available for inspection but cannot enter normal, semantic, stage,
or already-selected prompt rendering, and cannot pass automatic capability
activation. Other warnings, such as missing explicit validation language or
consuming over half the budget, remain visible without being mistaken for proof
that the advice is wrong.

An admission-held or explicitly quarantined winning-build example does not lock
its per-stack learning slot: a later eligible example can replace it. Eligible
reference-bearing winners remain unchanged on repeat wins. Static admission cannot
override a behavioral-verification quarantine. Replacement candidates are checked before
overwriting an incumbent; another held candidate leaves its content and receipt intact.

A changed body cannot reuse an old receipt. Missing, corrupt, or stale checks
do not grant eligibility, and failed receipt persistence is not reported as a
successful addition. The dashboard exposes the saved on-add result, timestamp,
and a Needs review filter, separately from content ratings and activation state.

Passing admission is **not measured effectiveness**, source approval, or permission
to activate. Imported capabilities still need their existing provenance/evidence
evaluation and activation; explicit rollback decisions remain in force. No source
code, bundled script, network call, or model evaluation is executed by admission.
Per-skill admission does not guarantee that several selected skills fit together;
whole-prompt packing and exposure receipts remain separate concerns.

Generate a complete JSON/CSV snapshot, including registered archived records:

```bash
.venv/bin/python -m skyn3t.intelligence.skill_catalog \
  --output docs/reports/2026-09-17-skill-audit \
  --prior-review docs/reports/2026-09-08-skill-audit.json
```

The optional prior review contributes editorial ratings only when the complete
body hash still matches. Archived content must match the retirement registry's
file and body hashes. Missing or changed archive evidence fails the export rather
than silently reducing its scope. API browsing includes the currently registered
library; retired and reference-only records remain in the export, not selectable
or activatable through the browser.

## Catalog roles and multi-stack matching

An imported agent-catalog role becomes one compact advisory skill. Its primary
stack stays in `Skill.stack` for compatibility, while every inferred stack is
also retained as a `stack:<name>` tag. The import additionally keeps the
catalog identity as `catalog:<id>` and preserves explicit stage tags.

Matching may use the primary stack or an explicit alternate `stack:<name>` tag,
but only after normal stack compatibility and quarantine checks. A role can
therefore serve several appropriate factory stacks without cloning the role or
letting an alternate stack tag bypass the eligibility fence.

### Local catalog activation

An imported local catalog is not active by default. Each compact role is stored
as a `catalog-candidate` with a quarantine tag, an advisory-body SHA-256, and a
relative source-path receipt. It remains visible for inspection but cannot be
selected for a build.

The catalog import API accepts `activate: true` as the explicit local trust
action. Activation validates the candidate state, body hash, and safe source
path, then replaces the candidate/quarantine tags with `catalog-promoted`.
Older persisted `agent_catalog` roles that lack this evidence load quarantined.
This keeps raw third-party catalog prose out of prompts until a user deliberately
activates the exact retained content.

## Narrow legacy GitHub quarantine

On load, SkyN3t recognizes one historical flattened-skill pattern: a literal
GitHub repository URL in `source` and a `github-distilled` tag. It is eligible
only when its canonical origin, full immutable Git object ID, evidence hash,
and source path validate together. A branch name, release label, or
`external-promoted` tag alone is not evidence. Otherwise it receives an
in-memory `hygiene:quarantine` tag and is excluded from default skill injection.
The original Markdown file is not rewritten.

This is deliberately narrow. It does not quarantine every GitHub URL or a
curated import with complete immutable evidence. GitHub-derived skills that
need to become eligible through the external-skill path still require the
separate immutable-provenance and explicit-promotion flow described in
[Evidence-backed learning](EVIDENCE_LEARNING.md).

## Practical inspection path

For a finished build, inspect the manifest's compact contract context, skill
receipts, and `stage_skills_used` entries together with its stage execution
records (including `execution.task.contract_digest`). That gives an auditable
answer to three different questions: which factory role ran, which advice it
received, and which frozen build decision it was expected to honor.