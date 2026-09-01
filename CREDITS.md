# Credits

SkyN3t borrows ideas — and in a few places, adapted prose and structure — from
other open-source projects. Both projects below are MIT licensed, so this file
reproduces their copyright and licence notices and records exactly what was
taken and where it landed.

---

## NousResearch/hermes-agent

<https://github.com/NousResearch/hermes-agent>

```
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Hermes' Mixture-of-Agents implementation is the direct model for SkyN3t's
advisory council. What was adapted:

| Mechanism | Source (hermes-agent) | Landed in |
|---|---|---|
| Tool-free advisor system prompt (not-the-actor, never-claim-execution, bad-vs-good examples) | `agent/moa_loop.py:233-262` | `_ADVISOR_SYSTEM` in `skyn3t/intelligence/council.py` |
| "Judge the state above" advisory trailer | `agent/moa_loop.py:947-952` | `_ADVISOR_TASK` |
| Guidance block handed to the aggregator | `agent/moa_loop.py:2042-2054` | `_GUIDANCE_HEADER` |
| A failed advisor becomes a sentinel, never fatal | `agent/moa_loop.py:574-583` | `AdvisorOutput(ok=False, error=...)` |
| Failed advisors filtered out of the aggregator prompt | `agent/moa_loop.py:1998-2018` | `CouncilEngine._assemble` |
| All advisors failed ⇒ the aggregator acts alone | `agent/moa_loop.py:2020-2031` | empty `guidance` ⇒ prompt identical to council-off |
| Bounded parallel fan-out with a worker cap | `agent/moa_loop.py:154-160, 732-830` | `asyncio.Semaphore(moa_max_concurrency)` |
| Per-advisor cost at that advisor's own model rate | `agent/moa_loop.py:163-214` | `AdvisorOutput.cost_usd` |
| Per-advisor output cap (latency ∝ output length) | `hermes_cli/moa_config.py:346-355` | `moa_advisor_max_tokens` |
| Head-truncation of oversized advisory material | `agent/moa_loop.py:216-224` | `moa_advisor_block_bytes` |
| Cache-stable tail injection of guidance | `agent/moa_loop.py:1303-1337` | tail of `CodeAgent._agentic_prompt` |
| Once-per-turn (`user_turn`) fan-out cadence as the default | `agent/moa_loop.py:1764-1782` | one council run per build, threaded |
| Static, multi-provider advisor slot list | `hermes_cli/moa_config.py:14-22, 194-222` | `skyn3t/adapters/model_slot.py` |
| Per-slot provider resolution independent of the acting model | `agent/moa_loop.py:313-338` | `complete(provider_override=...)` |
| Opt-in JSONL trace; tracing never breaks a turn | `agent/moa_trace.py:1-21, 97-167` | `skyn3t/intelligence/council_trace.py` |
| Reasoning-model timeout FLOORS + anchored slug matching | `agent/reasoning_timeouts.py:22-50` | `skyn3t/adapters/reasoning_timeouts.py` |

Deliberately **not** adopted, and why:

- **Virtual-provider swap** (`agent/agent_init.py:1065-1084`) — Hermes funnels
  every model call through one chat loop, so a facade there intercepts
  everything. SkyN3t's codegen goes through `LLMClient.agentic_build` (a CLI
  subprocess, or OpenRouter's own tool loop), so the analogous chokepoint would
  advise the cheap stages and miss codegen entirely.
- **`per_iteration` / `every_n` cadences** (`agent/moa_loop.py:1783-1856`) — four
  of five backends are one-shot subprocesses with no per-iteration injection
  point.
- **Degraded-mode notice** (`agent/moa_loop.py:2032-2041`) — their aggregator
  talks to a human and may need to disclose degradation; ours writes source files
  that ship, where "your advisors failed" is prompt noise.
- **Privacy filter** (`agent/moa_loop.py:24-150`), **credential pool**
  (`agent/credential_pool.py`), **`CanonicalUsage`** (`agent/usage_pricing.py`) —
  a local lab with one wire protocol does not need a PII redactor for advisor
  text, OAuth multi-key rotation for a hosted fleet, or a normaliser over six
  native provider adapters.

---

## garrytan/gbrain

<https://github.com/garrytan/gbrain>

```
MIT License

Copyright (c) 2026 Garry Tan

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

**Evaluated in depth, and deliberately not adopted.** gbrain was reviewed as a
possible source of Mixture-of-Agents design. It has none: "lens packs"
(`docs/architecture/lens-packs.md`) are schema/config namespacing, not
multi-perspective reasoning — a false friend.

Its genuinely strong ideas are all *judging* mechanisms:

- confidence-band-gated ensemble escalation (`src/core/cycle/grade-takes.ts`) —
  run one judge, escalate to a 3-model panel only when confidence lands in a
  borderline band;
- unanimity-plus-minimum-confidence before auto-applying a verdict, with
  "unresolvable" excluded from consensus (`aggregateEnsemble`);
- cross-provider judge panels with mean/floor thresholds and
  missing-dimension disqualification (`src/core/takes-quality-eval/aggregate.ts`);
- receipt-based eval contracts keyed on content-addressed hashes
  (`docs/eval-takes-quality.md`).

SkyN3t deliberately did **not** adopt any of them. This codebase's problem was
too many blocking checks, not too few — see `build_posture` in
`skyn3t/studio/gate_posture.py`. Adding a judge panel would have moved in
precisely the wrong direction. (SkyN3t's `golden_bench.py` already implements a
more rigorous version of the receipt idea independently.)

Credit is recorded here because the evaluation genuinely shaped the design — the
decision not to build a judging layer was informed by seeing what a good one
looks like.
