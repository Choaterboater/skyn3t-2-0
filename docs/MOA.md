# Mixture of Agents (advisory council)

N tool-free advisor models read the brief, stack and plan **before any code
exists** and hand private engineering guidance to the one agent that actually
writes the app. The coding agent is the *aggregator*: it holds every tool, it
authors every file, and the advisors only advise.

Adapted from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
(MIT). Full attribution, mechanism-by-mechanism, in [CREDITS.md](../CREDITS.md).

## Why it looks like this

Hermes makes MoA a *virtual provider*: selecting `provider="moa"` swaps a facade
in front of every model call, and the aggregator — which is also the acting model
— gets advisor text appended to its prompt each turn.

That shape does not transfer here. Hermes has one chat loop, so intercepting
`complete()` intercepts everything. SkyN3t's codegen does **not** go through
`complete()` — it goes through `LLMClient.agentic_build`, which either spawns a
CLI subprocess (`claude -p`, `codex exec`) or runs OpenRouter's own tool loop. A
facade at `complete()` would advise brainstorm, architect, critic and review —
the cheap stages — and miss codegen entirely. Backwards.

So the council runs as a step before the code stage instead. This is not a
downgrade: Hermes' own default and cheapest cadence is `user_turn` — advisors run
**once**, up front, and the acting model works alone afterwards
(`agent/moa_loop.py:1764-1782`). **A build is one user turn.** We implement that
topology exactly, and skip the two cadences (`per_iteration`, `every_n`) that are
physically impossible for four of five backends.

## Configuration

The council is **on by default**, advised by `claude_cli` and `kimi_cli`. Those
two are chosen because `auto` routes codegen to Codex first, so advising with
the other two makes the council genuinely multi-model instead of Codex reviewing
its own work. A slot whose CLI is not installed is recorded as a failed advisor
and the build proceeds on the survivors, so the default costs nothing on a
machine without them.

To turn it off, clear the advisor list (`SKYN3T_MOA_ADVISORS=""`) or flip the
master switch (`SKYN3T_MOA_ENABLED=0`). To change who advises:

```bash
# Let every CLI use its own configured default model.
SKYN3T_MOA_ADVISORS="codex_cli,claude_cli,kimi_cli"
# Or pin explicitly (verified working):
SKYN3T_MOA_ADVISORS="codex_cli,claude_cli:sonnet,kimi_cli:kimi-code/k3"
```

Addressable advisors: `codex_cli`, `claude_cli`, `kimi_cli`, `openrouter` — and
`copilot_cli`, which is supported but deliberately left out of the default
`auto_cli_priority` chain. Each slot takes an optional `:model`; omit it to use
that CLI's own default (Codex reads `~/.codex/config.toml`, Kimi reads
`default_model` from its `config.toml`).

**A pinned model must use that CLI's own alias form.** Kimi wants the
fully-qualified config key — `kimi_cli:kimi-code/k3`, not `kimi_cli:k3`, which
exits with `Model "k3" is not configured in config.toml`. A model id containing
`/` parses correctly: only a *known provider* prefix is split off, so
`kimi_cli:kimi-code/k3` → provider `kimi_cli`, model `kimi-code/k3`. When a pin
is wrong the slot is simply recorded as a failed advisor and the build proceeds
on the survivors.

> **Model ids churn.** Do not copy an example verbatim and assume it is current.
> Check your CLI's own aliases, or the live catalog:
> `curl https://openrouter.ai/api/v1/models`. Nothing in the code depends on a
> specific model name — the one place that names models
> (`skyn3t/adapters/reasoning_timeouts.py`) is a pure accelerator that returns
> `None` for anything unlisted, leaving your configured timeout untouched.

| Setting | Default | Meaning |
|---|---|---|
| `moa_enabled` | `true` | Master switch. Set `SKYN3T_MOA_ENABLED=0` to force off. |
| `moa_advisors` | `"claude_cli,kimi_cli"` | Comma-separated `provider:model` slots. Model optional. Empty ⇒ off even when enabled, and clearing it is the ordinary off-switch. Excludes the usual acting model on purpose. |
| `moa_max_concurrency` | `4` | Concurrent advisor calls (per-provider limits still apply underneath). |
| `moa_advisor_timeout` | `60` | Seconds per advisor, then **dropped**. Bounds — but does not eliminate — added build latency; see Cost. |
| `moa_advisor_max_tokens` | `1200` | Output cap. Binds on OpenRouter only — `complete()` cannot pass `max_tokens` to a CLI. |
| `moa_advisor_block_bytes` | `3000` | Hard per-advisor byte budget for the assembled guidance. |
| `moa_trace_enabled` | `false` | Append a full-fidelity JSONL line per run under `logs_dir/moa/`. |

Slot addressing (`skyn3t/adapters/model_slot.py`): a prefix is consumed as a
provider **only** when it names a known one, so `claude_cli:sonnet` splits but
`deepseek/deepseek-chat` stays a whole model id meaning "pin this model on the
active backend" — exactly what `model_override` already meant.

Providers may differ per slot. That is the entire point: an advisor on Claude, an
advisor on DeepSeek via OpenRouter, and an advisor on Codex can all run at once.

## Guarantees

- **Advisors can never break a build.** One fails, some fail, the provider is
  signed out, the whole council raises — the build proceeds. When *every* advisor
  fails, `guidance` is empty and the codegen prompt is **byte-identical** to a
  council-off build.
- **No gate.** The council never inspects output, never scores, never blocks,
  never touches the verdict. It is a capability layer.
- **Off on the stub backend.** Keeps the offline test suite's codegen prompts
  byte-stable and offline builds at $0 — and stops a stub's canned
  "Offline response." text being injected as if it were advice.
- **One run per build, threaded.** Not recomputed per best-of-N trajectory: that
  would multiply spend *and* advise each candidate differently, destroying the
  premise that trajectories differ only by model.
- **Guidance goes at the tail** of the codegen prompt. The directive block above
  it is byte-identical across every build of a given stack; splicing
  brief-varying text into the middle would fragment the longest span shared
  across builds and across trajectories.

## Cost

Honestly: this is **N extra completions per build**, on top of codegen. Three
advisors at ~1200 output tokens each is real spend on a build that previously
cost one codegen session.

**`per_build_usd_cap` does not contain a CLI council.** The contextvar plumbing
is real — advisors share the build's `BudgetTracker` and cannot escape the
accounting context — but a CLI backend reports no price: `_cli` hard-codes
`cost_usd=0.0, cost_source="not_reported_by_cli"` (`adapters/llm.py:2802`) on
every path, success and failure alike. So two `claude_cli`/`kimi_cli` advisors
add exactly **$0.00** to `spent_build` no matter how much subscription they
consume, and any cap you set is compared against zero forever. The cap binds
**OpenRouter advisor slots only**. (`per_build_usd_cap` also defaults to `0.0`,
which means *disabled*, not *zero spend allowed*.)

What actually bounds a CLI council is not currency:

- `moa_advisor_timeout` (60s) × `ceil(N / moa_max_concurrency)` — the council is
  awaited **inline before codegen**, so this is added build latency, not
  background work
- `cli_max_concurrency` admission, and one fan-out per build over exactly the
  slots you named
- `daily_token_cap`, via an estimated `len(text) // 4` — also disabled by default

The honest summary: for signed-in CLI advisors the real cost is subscription
rate-limit consumption and wall clock, and SkyN3t cannot meter either.

Slots dropped by `free_only` (a paid OpenRouter pin) or `no_claude` are recorded
in `manifest.extra["moa"]["dropped"]` with a reason, so a council that looks
configured but ran empty is visible rather than silent. `free_only` deliberately
does **not** filter CLI slots: a signed-in CLI bills a subscription the operator
already holds.

## What it produces

`manifest.extra["moa"]` — bounded, no advisor prose:

```json
{
  "advisors": [
    {"label": "claude_cli:sonnet", "ok": true, "chars": 1840, "cost_usd": 0.0, "duration_ms": 4210.3, "error": ""},
    {"label": "kimi_cli", "ok": false, "chars": 0, "cost_usd": 0.0, "duration_ms": 118.0, "error": "provider unavailable (degraded to stub)"}
  ],
  "failed": ["kimi_cli"],
  "dropped": [],
  "guidance_chars": 2034,
  "cost_usd": 0.0,
  "degraded": true
}
```

With `moa_trace_enabled`, `logs_dir/moa/<build_id>.jsonl` additionally carries
each advisor's full text and the assembled guidance, for offline audit. Those
files are disposable.

## Related

`skyn3t/intelligence/debate.py` is a different shape and stays separate: N
*peers* propose → cross-examine → vote → a synthesiser merges, producing a
*winner*. MoA is N *advisors* → one *actor*. Debate previously ran every debater
on the same routed model — one model arguing with itself — and now takes the same
`ModelSlot` list so it is a real ensemble:

```bash
skyn3t debate "which cache strategy?"   # uses slots when configured
```
