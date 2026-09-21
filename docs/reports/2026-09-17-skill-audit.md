# Skill inventory and prompt-budget audit - 2026-09-17

## Scope and limits

The final snapshot contains **1,852 records: 1,560 loaded skills, 82 reference-only
archives, and 210 retired archives**. The background learner added 100 skills
during the work; the initial 1,752-record count is therefore superseded by this
timestamped snapshot. The browser assesses new arrivals automatically on its next
refresh; the CSV/JSON remain dated snapshots.

Every retained body was scanned in full by deterministic rules. These are
**provisional prompt-readiness ratings, not 1,852 independent expert reviews or
effectiveness benchmarks**. Keyword and structure checks can miss technical errors
or misunderstand examples. No fresh upstream verification or paid model evaluation
was performed.

- [Sortable per-skill CSV](2026-09-17-skill-audit.csv)
- [Full JSON, reasons, hashes, evidence states, and duplicate relationships](2026-09-17-skill-audit.json)

The audit preserves **330 prior editorial reviews whose complete body hashes still
match**: 38 loaded skills and 292 archives. Their original ratings and rationales
remain separate from the new prompt-readiness ratings.

## Ratings

| Prompt readiness | Loaded | Archived | Total |
|---|---:|---:|---:|
| 4/5: compact procedure with scope/validation/failure signals | 21 | 0 | 21 |
| 3/5: useful advisory text with gaps or substantial prompt cost | 77 | 58 | 135 |
| 2/5: reference extraction, sparse guidance, or needs distillation | 1,459 | 231 | 1,690 |
| 1/5: unsuitable as-is for this prompt budget or a weak example | 3 | 3 | 6 |
| **Total** | **1,560** | **292** | **1,852** |

The heuristic never awards 5/5. A high rating does not activate a skill, certify
its source, establish technical correctness, or prove a contribution to build
quality. Conversely, a low prompt-readiness rating does not mean the source
project or the full document is poor.

## Main findings

**1,421 retained bodies use the repository-reference extraction template.** These
may contain useful material, but installation fragments, repository descriptions,
contribution templates, and partial reference notes are not equivalent to scoped,
independently reviewed build procedures. They are flagged for distillation rather
than automatically recommended as default prompt guidance.

**145 records consume more than half of the global 6,000-character advice budget,
and 5 exceed the entire budget.** The oversize population includes 2 loaded skills
and 3 archived records. There are also **22 exact duplicate-body groups**. Duplicate
relationships are recorded without deleting or merging anything.

| Skill | Prompt readiness | Rendered chars | Finding |
|---|---:|---:|---|
| `mcp-implementation-and-evaluation` | 4/5 | 1,889 | Compact scoped MCP procedure; effectiveness remains unmeasured |
| `mcp-server-contract` | 3/5 | 309 | Efficient protocol checklist, but not a complete implementation procedure |
| `won-mcp-shape` | 1/5 | 3,516 | Calls a 37-point build a winner, contains the legacy FastMCP import, and consumes 58.6% of the budget |
| `desktop-editor-layout` | 3/5 | 2,163 | Useful concrete layout guidance; 36% of the budget and no explicit verification step |
| `game-feel-juice` | 1/5 | 11,421 | Strong long-form content cannot fit intact in the current shared advice budget |

The prior body-matched editorial ratings are **4/5 for `desktop-editor-layout`**
and **5/5 for `game-feel-juice`**. Their lower prompt-readiness ratings illustrate
why content quality and suitability for a bounded prompt must remain separate.

The recorded MCP build selected seven skills, but its 6,000-character global
advice truncation omitted five selected blocks. Selection receipts and historical
build scores must not be treated as proof of complete exposure or causal benefit.
This audit does not change that packing behavior or substitute a different model.

## Organization delivered

The Skills dashboard now starts with clickable categories. Search narrows
categories and skills by title, description, category, and topic. Category,
topic, status, rating, and size filters combine; selecting a category preserves
the other filters. Topic chips are clickable, and results are shown 36 at a time
with an explicit show-more control.

Each card separates provisional readiness, character cost, historical build
association, and capability evidence. The complete body hash and rating reasons
are available in its detail section. `desktop-editor-layout` appears under
**Desktop & mobile** and **Frontend & UI**.

No skill bodies, selection scores, activation states, or quarantine rules were
changed by the audit/browser implementation. Categories are navigation metadata,
not new build eligibility. Archived records are included in the export but cannot
be activated from the live browser.

## Remaining factory issues

The audit identifies but does not fix the global advice truncation, weak-build
example promotion, exposure-based grading, or Union Alpha generation failure.
Balanced whole-skill prompt packing and post-packing exposure receipts are still
needed before selected-skill lists can accurately describe what a builder saw.
