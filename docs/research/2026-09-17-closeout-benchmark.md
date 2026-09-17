# Closeout measurements and audit — 2026-09-17

Scope, in one line: **council-only wall-clock policy comparison plus the
repository's deterministic self-audit**. No end-to-end build-speed claim is
made, and none is established.

## What was run

1. **SkyN3t self-audit (deterministic, `--no-llm`):**
   `python3 -m skyn3t.cli.main audit product --no-llm --out
   docs/audits/2026-09-17-closeout.md --json-out
   artifacts/audits/2026-09-17-closeout.json` — overall **95.2/100**;
   the only P3 is "STATUS.md was last reviewed 2026-08-01" (fixed this
   session; the header now reads 2026-09-17).
2. **Dead-code scan:** `ruff check --select F401,F811,F821,F841` found one
   unused import in `tests/test_lesson_injection_ranking.py`
   (`_summarize_outcome`, left over from the outcome-only lesson-gate work);
   removed and the file's two tests re-run green. `vulture --min-confidence
   100` flags five 100%-confidence names, all inspected and **kept**: they are
   interface conformance (no-op metric `amount` parameters, context-manager
   `exc_info`, deliberately reserved `attended`/`plan_dict` parameters whose
   call-site contract is documented in-source).
3. **Golden control (stub backend, inline execution, 1 repeat,
   `restaurant-static`):** `docs/research/2026-09-17-stub-control.md` —
   **completed, 0/1 passed** (verdict `no_go`, score 34). Proof ladder could
   not run its `docker` step (`proof_ladder.unavailable`), so the release
   gate correctly blocked the scaffold. The harness works; verified delivery
   needs Docker on this machine.
4. **Live council policy comparison (5 matched trials each, same brief,
   same configured free OpenRouter advisors
   `nvidia/nemotron-3.5-lightning:free` + `dots-studio/dots-3-note-preview:free`,
   interleaved order):** `artifacts/golden/2026-09-17-council-policy.json`.
   - `full` median **30.0 s** (per-advisor timeout × waves) with usable
     guidance in **4/5** trials (1 cancelled at deadline).
   - `bounded` median **15.0 s** (`moa_council_timeout`) with usable guidance
     in **0/5** runs — every advisor was cancelled at the 15 s deadline.
   - Reported advisor cost: $0.00 in both arms (free models).
   - **Conclusion:** the bounded policy halves the council's wall-clock cost
     but, at this deadline and with these free advisors, drops ALL advice.
     The 15 s default is too tight for these advisors; the existing
     `best_quality`/`full_app` full-council default stays correct for quality,
     and operators who want the speed can raise `moa_council_timeout`. This
     is a measured tradeoff, not a speedup to ship.
5. **Full static suite:** **5,353 passed / 10 skipped** (two independent
   runs, 648s/652s) before the benchmark; targeted suites re-run after the
   cleanup (77 + 179 + 2 tests) all green. Dashboard production build and
   wheel integrity check (`277 members, 40 UI files`) PASS.

## Explicitly not established

- **No end-to-end verified-delivery speedup** for either policy or for the
  outcome-verification work: the golden control proves the harness blocks
  correctly when Docker is absent, which makes a matched verified-delivery
  timing comparison impossible on this machine today.
- **No quality claim for either council policy**: 5 short trials, one brief,
  free models; the bounded arm produced no advice at all, so there is nothing
  to compare for quality.
- The `restaurant-static` stub failure is an environment gate (missing
  Docker), not a product regression; the same suite passed under the stub
  backend in the committed golden tests.

## Reusable commands

```bash
# Self-audit (deterministic, no LLM)
python3 -m skyn3t.cli.main audit product --no-llm \
  --out docs/audits/<date>.md --json-out artifacts/audits/<date>.json

# Dead code
python3 -m ruff check skyn3t --select F401,F811,F841
uvx vulture skyn3t --min-confidence 100

# Council policy timing (5 matched trials, live free models)
env PYTHONPATH="$PWD" python3 /tmp/skyn3t-council-benchmark.py
```

The council benchmark script is intentionally in `/tmp`: it is a one-off
measurement harness, not product code.
