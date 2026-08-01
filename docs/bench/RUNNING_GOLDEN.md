# Running the Golden Benchmark

The golden benchmark is the repeatable product-quality gate for generated apps. It uses the
same `StudioRunner` path as a normal build, then checks committed expectations for each case
across independent repetitions. It is separate from `docs/bench/go-rate.*`; golden commands
never replace those historical reports.

## Safety Defaults

Golden runs default to the deterministic `stub` LLM backend and the `inline` execution backend.
They do not require paid credentials. Each case and repetition receives its own project slug,
the runner's per-build budget is reset, and the daily budget ledger remains enforced. Repeats
are limited to 1-10. Attempt-local state removes host repository credentials and skill paths and
disables paid assets and remote deployment. Every other non-secret Settings value copied into an
attempt, including nested model-routing controls, retry policy, and sandbox hardening, is part of
the compatibility fingerprint.

The packaged default suite is `skyn3t/benchmarks/golden-v1.json`. Installed wheels locate it
automatically, so the normal validation command is:

```powershell
skyn3t bench golden validate
```

Validation parses and normalizes the full suite, prints its pinned digest, and exits nonzero for a
malformed or unsafe contract. Pull requests run this validation only; they never launch the
expensive build loop.

## Run

```powershell
skyn3t bench golden run `
  --out artifacts/golden/run.json `
  --report artifacts/golden/run.md `
  --seed 20260709 `
  --repeats 2 `
  --execution-backend inline `
  --llm-backend stub
```

The JSON ledger records the commit, working-tree dirty flag and status digest, platform, Python
version, execution and LLM backends, seed, repeat count, suite digest, exact per-case check
contracts, and deterministic metadata fingerprint. Commit and dirty-state provenance are
deliberately not comparison inputs, so different source revisions remain comparable. The
Markdown report contains the aggregate pass rate and Wilson interval, per-stack and per-case
results, and failed expectations. Output is written atomically. An interrupted run leaves an
explicitly partial/error ledger rather than a passing result.

`golden run` exits `0` only when every contract passes, `1` when a completed ledger contains
one or more failed attempts, and `2` for invalid input or a partial/error run. A failed contract
still leaves complete JSON and Markdown evidence for comparison and upload.

Use a fixed seed and repeat count when comparing runs. Changing either alters the metadata
fingerprint and makes the results unsuitable for promotion gating.

## The design suite

A second packaged suite, `skyn3t/benchmarks/golden-design-v1.json`, examines design
distinctiveness instead of general coverage: five briefs that each demand a specific,
non-default aesthetic (brutalist zine, warm bakery editorial, Swiss dashboard, art-deco
hotel, Y2K portfolio), so generic output fails visibly. It shares the v1 schema and the
same runner; the only contract difference is that per-case `min_intent_score` floors may
sit below 80 — the schema floor is 60 — because style-direction words legitimately never
appear as page copy on a correct delivery.

Run it exactly like the default suite, pointing `--suite` at the file. For a real design
measurement you want live codegen, not the stub floor:

```powershell
skyn3t bench golden run `
  --suite skyn3t/benchmarks/golden-design-v1.json `
  --codegen-cli codex `
  --out artifacts/golden/design.json `
  --report artifacts/golden/design.md `
  --repeats 1
```

Beyond score and gates, compare the advisory AI-look warnings recorded per build at
`manifest.extra.web_polish.warnings` (indigo gradients, Inter-first type, glassmorphism,
placeholder copy, full-viewport heroes, identical card grids) — that is the direct "does
it still look generated" signal. The two invitation-dependent detectors (emoji, identical
card grid) are brief-aware: a brief asking for playful stickers or a gallery grid does not
false-flag its own delivery. Live runs are non-deterministic; compare trends over several
seeds/repeats, not single runs.

## Compare

A baseline must come from a real completed run of the same suite. Do not hand-author it. Once a
reviewed baseline is checked in or restored from a prior workflow artifact, run:

```powershell
skyn3t bench golden compare `
  --baseline benchmarks/golden-baseline-v1.json `
  --candidate artifacts/golden/run.json `
  --out artifacts/golden/comparison.json `
  --report artifacts/golden/comparison.md `
  --max-suite-pass-rate-drop 0 `
  --min-case-pass-rate 1
```

Comparison exits nonzero when the suite digests differ, either ledger is partial/invalid, the
suite pass rate crosses the allowed regression threshold, or a case falls below its minimum
pass rate. A failing comparison still writes JSON and Markdown evidence.

## GitHub Actions

`.github/workflows/golden-bench.yml` has three entry paths:

1. Pull requests validate `skyn3t/benchmarks/golden-v1.json` without building apps.
2. The weekly schedule runs the exact repository suite with `stub` and `inline`.
3. Manual dispatch accepts bounded repeats, a seed, and an optional checked-out baseline path.

Run and comparison artifacts upload under `artifacts/golden/` even when the regression gate
fails. Until `benchmarks/golden-baseline-v1.json` is generated from a reviewed run, scheduled
runs publish evidence and clearly record that comparison was skipped. The run's own contract
status remains an unconditional gate, so a missing baseline can never turn a failed suite green.

## Promoting a Baseline

1. Download a successful workflow's `run.json` and `run.md` artifacts.
2. Confirm the suite digest, clean working-tree provenance, metadata fingerprint inputs,
   failures, and Wilson interval.
3. Re-run the same seed and repeats locally or in a second workflow dispatch.
4. Check in the reviewed JSON as `benchmarks/golden-baseline-v1.json`.
5. Run `golden compare` once more against that checked-in file before merging.

Never promote a partial run, a run from a dirty working tree, a run using paid or unrecorded
configuration, or a run whose suite digest differs from the current repository suite.
