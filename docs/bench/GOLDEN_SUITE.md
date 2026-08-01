# Golden Suite v1

The Golden Suite is SkyN3t's fixed product-quality exam. It defines what a
successful build must produce; it is not a report of results from a previous
run. A failed case is a product regression to investigate, not a reason to
weaken that case's thresholds.

## Canonical data

The only suite definition is the packaged resource
`skyn3t/benchmarks/golden-v1.json`. Keeping it inside the Python package makes
the same 30 cases available from a source checkout, an installed wheel, and CI.
Do not copy the suite into docs, workflow YAML, or a second root-level JSON
file. Consumers should resolve the default through the golden benchmark module.
The canonical version 1 digest is
`b9e9d51b8c7790b31ecad15bb02c8cadcb4995fd9685d94a46ee81438ab3d4c7`.

The suite contains exactly 30 concrete briefs:

- Every one of the 19 keys in `REAL_BUILDER_STACKS` has at least one case.
- Higher-risk families have additional cases: FastAPI (4), Next.js (3), and
  React, static, Python CLI, Phaser, RAG, and agent packs (2 each).
- Ten cases explicitly exercise scaffold variants, including Three.js,
  market-data, paper-trading, model-router, terminal-copilot, Supabase auth,
  local backend, persistent memory chat, and the dino runner.
- Domains span commerce, publishing, documentation, support, billing,
  inventory, hospitality, developer tools, finance, games, retrieval,
  automation, security, mobile, and native desktop software.

## Version 1 schema

The top-level object has these exact fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer schema version; currently `1`. |
| `suite_id` | Stable lowercase identifier (`golden-v1`). |
| `name` | Human-readable suite name. |
| `description` | Scope and purpose, without measured claims. |
| `cases` | Ordered list of acceptance cases. |

Each case has a stable `id`, a concrete `brief`, a canonical pinned `stack`,
searchable `tags`, and an `expectations` object. Expectations declare:

- `expected_stack`: the canonical builder that must be delivered.
- `min_score`: minimum reviewer/proof score. Version 1 uses `60`.
- `min_intent_score`: minimum brief-to-delivery intent score. Version 1 uses
  `80`; matching the stack alone is not enough.
- `required_gates`: deterministic checks that must be recorded for the case.
- `required_artifacts`: exact project-relative POSIX paths that must exist.

Artifact paths are literal, not globs. They may not be absolute, contain `..`,
use backslashes, or name generated dependency/build directories. Prefer stable
entrypoints, manifests, pure cores, and tests from the deterministic scaffold.
Run ledgers embed the exact ordered check-name contract for every case. A
non-error attempt is invalid unless it contains every invariant, gate, and
artifact check exactly once and in that order.

## The design suite (golden-design-v1)

A second packaged suite, `skyn3t/benchmarks/golden-design-v1.json`, lives beside
the canonical one and shares the v1 schema. It measures design distinctiveness:
five briefs with named, non-default aesthetics. Its per-case intent floors may
drop below the version-1 `80` — the schema floor is `60` — because
style-direction vocabulary legitimately never appears in delivered page copy;
each case documents its own floor. It is additive: golden-v1 remains the
promotion gate.

## Gate policy

Every case requires `proof`. Cases supported by the generated-app security
scanner also require `security_check`, and public HTML stacks require the
deterministic `seo` result. A required gate must execute; soft-skipped checks are
not listed merely because a broader runner registry mentions the stack.
Stack-specific contracts add `headless_gate`, `mcp_check`, `rag_check`,
`workflow_check`, `cli_check`, or `cli_playtest` where they apply.

Visual game judgement, QA playtests, and deployed-URL checks are intentionally
not required by this suite: they depend on optional browser, vision, deployment,
or host capabilities. Those checks remain useful in their dedicated workflows,
but absence of optional infrastructure must not change the fixed core exam.

## Change discipline

1. Keep case IDs stable so historical runs remain comparable.
2. Add a new case only for a distinct builder, variant, or recurring production
   failure; keep the suite bounded and balanced.
3. Verify required artifacts against `scaffold_for` and gates against the stack
   registry before changing the JSON.
4. Never encode secrets, host-specific paths, timestamps, or measured outcomes.
5. Do not lower score or intent floors to turn a failure green. Fix the builder,
   scaffold, prompt, gate, or generated application and rerun the case.
6. A schema change requires a new version, loader support, data tests, and an
   explicit migration note.

The executable workflow and command-line usage are documented separately in
`docs/bench/RUNNING_GOLDEN.md`.
