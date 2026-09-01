# API / Config Logic Debug Report

Date: 2026-07-31
Method: 6-agent read-only debug swarm over `skyn3t/config/settings.py`, `skyn3t/adapters/llm.py`,
`skyn3t/core/model_router.py`, `skyn3t/security/secrets.py`, `skyn3t/agents/config_detector.py`,
`.env.example`, and a consumer sweep of `skyn3t/web/`, `skyn3t/agents/`, `skyn3t/studio/`.
Top findings were re-verified by reading the cited lines. No source code was modified.

## Summary

| Severity | Count | Theme |
|---|---|---|
| High | 1 | Silent fake-success on Windows CLI codegen (kimi/copilot argv prompts) |
| Medium | 17 | Silent degradation to stub, cost-guard bypasses, precedence drift, secret-redaction gaps |
| Low | 20 | Misleading logs/messages, validation gaps, dead config, minor mis-ranking |

## High severity

### H1. kimi/copilot agentic codegen prompt goes over argv — Windows truncation silently fakes success
- `skyn3t/adapters/llm.py:3769-3773`, `_CLI_STDIN_PROMPT = frozenset({"codex", "claude"})` at `llm.py:195`.
- The code itself documents the failure mode (`llm.py:3750-3754`): a cmd.exe npm-shim truncating a
  multi-KB argv prompt "fails silently, returning a plausible short reply that counts as a successful
  build." Only codex/claude got the stdin safeguard; kimi (also npm-shipped on Windows) and copilot
  still pass the full codegen prompt — the largest in the system — on argv (`--prompt <full>` / `-p <full>`).
- Fix: add kimi (and copilot if it supports stdin) to `_CLI_STDIN_PROMPT` with argv
  `[] if stdin_prompt else [prompt]`, mirroring the claude branch at `llm.py:3757`.

## Medium severity

### Config / settings

**M1. Corrupt-but-valid-JSON tuning overrides crash `Settings()` construction** — `settings.py:71-78`.
The `try/except` only wraps `load_overrides`; the allow-list filters keys, not value types. A
`settings_overrides.json` containing `{"best_of_n": "not-an-int"}` raises `ValidationError` from
every `get_settings()` caller, contradicting the "never let tuning break config construction"
docstring. Fix: trial-validate and drop offending keys, or extend the guard to cover validation.

**M2. Tuning overrides read/write roots diverge** — read from `REPO_ROOT / "data"` (`settings.py:74`)
but written to `settings.data_dir` (`cli/main.py:1113`, `cortex/bootstrap.py:101`). With
`SKYN3T_DATA_DIR` set, persisted tuning is silently never applied. Fix: anchor both sides to the
same root.

**M3. `llm_backend` is an unvalidated free string** — `settings.py:138`. A typo like
`SKYN3T_LLM_BACKEND=claude` (missing `_cli`) fails the `SUPPORTED_LLM_BACKENDS` check
(`llm.py:1788-1789`) and silently degrades the whole system to the offline stub. Same gap for
`execution_backend` (`settings.py:628`) and `game_art_source` (`settings.py:322`).
Fix: `Literal[...]` as `build_posture` already does (`settings.py:595`).

**M4. `.env.example` drift** — `.env.example:100` pins `SKYN3T_BEST_OF_N=1` vs code default 2
(`settings.py:369`); `.env.example:27` sets `SKYN3T_CLI_LLM_TIMEOUT=180` vs default 300
(`settings.py:195`). Copying the example silently changes out-of-box behavior.

### LLM adapter

**M5. CLI child env strips the CLIs' own auth keys** — `llm.py:225-232`. `filter_env()` removes
`ANTHROPIC_API_KEY`, `KIMI_API_KEY`, `OPENAI_API_KEY` etc., so a CLI that authenticates via env var
(not interactive login) exits non-zero → `_failed_cli_fallback` → stub counts as a result. The
matching `settings.*_api_key` values are never injected into the child env — dead for the CLI path.
Fix: re-inject the one matching key per provider, or document env-key auth as unsupported.

**M6. `SKYN3T_ANTHROPIC_API_KEY` / `SKYN3T_OPENAI_API_KEY` / `SKYN3T_KIMI_API_KEY` are dead config
surfaced as live** — `.env.example:79-81`, `settings.py:102-104`, `web/routes.py:4935-4940`,
`Settings.jsx:43`. No backend consumes them; the dashboard collects them, reports them "configured",
and `Settings.has_any_llm` (`settings.py:671`) counts them, so `/api/status` claims LLM availability
while generation stays on stub. Fix: wire the backends or stop presenting them as functional.

**M7. `require_codex_for_auto` hard-requires Codex, ignoring `auto_cli_priority`** — `llm.py:578-583`.
A host with only Claude CLI signed in — which `.env.example:15-17` says `auto` supports — is blocked
with "Automatic builds require Codex CLI on PATH". Fix: require any available CLI in
`auto_cli_priority`.

**M8. Explicitly selected backend silently degrades to stub** — `llm.py:1795,1798`. Picking
`openrouter` without a key (or an unavailable CLI) returns `"stub"` with no log at resolve time;
only `backend_status()` carries the reason. Same for an unknown `provider_override`
(`llm.py:1789-1791`) — a typo'd MoA advisor slot silently injects offline-stub "guidance" into
codegen. Fix: `log.warning("llm.backend_degraded_to_stub", ...)` at both points.

**M9. Last fallback candidate never quarantined; single-fallback case retries it twice** —
`llm.py:2284-2292`. `_mark_model_unhealthy` only runs when `ci + 1 < len(candidates)`, so a
permanently broken final candidate burns the full retry budget on every call forever; with exactly
one fallback it is re-appended and retried twice in one call. Fix: mark unhealthy on loop exit and
skip already-tried models when extending.

**M10. `free_only` cost guard not enforced for a configured paid vision model** — `llm.py:2549-2551`.
Text pins under `free_only` are *dropped* (`llm.py:1388-1390`); the vision path only logs a warning
then `return configured, True`, silently billing a paid model. Verified by reading the branch.
Fix: drop the image or gate behind an explicit opt-in flag.

**M11. `BudgetExceeded` reused for ledger-lock I/O failures** — `llm.py:1101-1130`. Callers treat the
type as "spend cap hit" (`llm.py:3191-3193`), and a lock hiccup can raise it from the `LLMClient`
constructor. Fix: distinct `BudgetLedgerError`.

**M12. Budget-ledger write failure swallowed with no log** — `llm.py:1280-1281` (`except OSError: pass`).
Cross-process daily-cap enforcement silently stops; every other silent-degrade path in the file logs.
Fix: throttled `log.warning`.

**M13. `agentic_idle_timeout=0` means opposite things in the two agentic paths** — OpenRouter path
(`llm.py:3039,3106`): 0 disables the watchdog. CLI path (`llm.py:3816-3819`): 0 means "use the total
timeout as idle timeout". Same setting, contradictory semantics. Fix: one convention (0 = disabled).

**M14. Stale operator-facing reason string** — `llm.py:2067-2071` claims auto "never falls back to
OpenRouter", contradicting `auto_cli_priority` + `auto_allow_openrouter` behavior
(`llm.py:1805-1811`). Related: `backend_status()["preferred_cli"]` mislabels `auto` (`llm.py:2049-2053`).

### Model router

**M15. `_openrouter_catalog_allowed()` ignores the `auto_allow_openrouter` path** —
`model_router.py:792-803`. With `llm_backend="auto"` + `auto_allow_openrouter=1` + key, dispatch
resolves backend `"openrouter"` but the router checks raw `settings.llm_backend == "openrouter"`,
so under the default `free_only=True` the live-catalog free-model self-heal never runs and retired
hardcoded `:free` defaults are sent to the API — while `_fallback_models` (`llm.py:2194`) uses the
resolved backend. The two layers disagree on the same backend. Verified by reading both sites.
Fix: key catalog permission on the effective resolved backend.

**M16. Failed catalog fetches cached as empty for the full 1 h TTL** — `model_router.py:159-163,
198-201`; also primed empty from `web/routes.py:5683,5689`. One transient network error disables
live paid ranking and free self-heal for an hour. Fix: keep the previous good cache on error or use
a short error TTL; don't prime `[]` stamped `now`.

**M17. Cheap-tier price promise bypassed through the paid-fallback cache** — `model_router.py:822-825`.
No cheap-eligibility check on cached entries; the live `data/model_router_paid_fallback.json`
currently contains `"cheap": "openai/gpt-5.6-luna"` (premium-class) on a route that elsewhere
enforces ≤$1/M-in/≤$3/M-out. Fix: only accept cache entries passing `auto_model_allowed` when
`_requires_cheap_price`.

### Secrets / detector

**M18. `SecretsStore` seeding omits deploy + messaging tokens** — `secrets.py:73-76`. The six deploy
tokens (`settings.py:125-130`) and messaging tokens can't be redacted by `redact()`/`scrub_text`;
`golden_bench.py:96-112` already documents them leaking verbatim into `run.json`. Compounded by
`studio/runner.py:288` constructing `AuditLog` with no `SecretsStore`, so non-regex secrets land
verbatim in `logs/audit.jsonl`. Fix: seed from `settings.deploy_tokens()`; pass a store to `AuditLog`.

**M19. config_detector can ship secrets client-side** — `config_detector.py:110-115` (server-forcing
misses `*_WEBHOOK_URL`, though `secrets.py:23` lists "webhook" as a secret marker) and
`config_detector.py:189-204` (`_normalize_spec` doesn't re-apply kind-based server-forcing to
LLM-returned specs). Also: Supabase branch hardcodes `NEXT_PUBLIC_*` for all stacks
(`config_detector.py:126-137`) and `_client_prefix_for` maps everything non-Next to `VITE_`,
ignoring Astro `PUBLIC_` / CRA `REACT_APP_` (`config_detector.py:84-87`).

### Consumers

**M20. `render` deploy provider unreachable from the GUI** — `web/routes.py:4942-4964`. Settings and
`DeployAgent` fully support render, but the three provider maps in routes.py omit it, so
`set_deploy_credential("render", ...)` raises "unknown deploy provider".

**M21. GitHub explorer/ingestor ignore the GUI-managed token** — `github_explorer.py:88`,
`github_ingestor.py:272-273` read only `GITHUB_TOKEN`/`GH_TOKEN`, while the dashboard writes
`SKYN3T_GITHUB_TOKEN` and every other consumer checks it first. Silent rate-limit degradation.
Fix: mirror `github_fetch.py:30-37`'s chain.

**M22. `set_llm_routing(persist=False)` skips `os.environ` despite its docstring** —
`web/routes.py:6215-6271`. Sibling setters write env unconditionally and gate only the `.env`
persist; a running cortex reading env won't see a non-persisted routing change.

**M23. Inverted credential precedence for channel tokens** — `integrations/channels.py:72-75`
checks the bare name first; everything else is SKYN3T-first. A stale bare `TELEGRAM_BOT_TOKEN`
silently shadows the GUI-saved token for sending while the status payload reports the SKYN3T one.

**M24. Config-UI stack families miss planner stacks** — `config_ui_agent.py:35-38` omits `react_ts`,
`sveltekit`, `vuejs`, `typescript` (all emittable by `planner.py:37-49`), producing self-inflicted
"app needs client config but has no settings UI" warnings.

## Low severity (selected; all verified by the swarm with file:line)

- `llm.py:2167-2168` — retry jitter added *after* the cap; delay can exceed `llm_retry_max_delay` by 50%.
- `llm.py:2752-2758` — timeout log reports unfloored `cli_llm_timeout`, understating the real budget.
- `llm.py:3014,3786-3790` — `timeout or <default>` remaps explicit `timeout=0`; `max(4, ...)` makes
  `openrouter_agentic_max_turns < 4` impossible (`llm.py:3011`).
- `llm.py:4268,1535` — paid-cost estimation only fires when *both* token counts are 0; partial usage
  understates cost.
- `llm.py:3167-3196` — result status mutated to `malformed_response` *after* being recorded to the budget.
- `llm.py:857-869` — `_to_data_url` hardcodes `data:image/png` for every local file (JPEG/WebP mislabeled).
- `llm.py:1820-1824` vs `4187` — key revoked mid-build yields `Authorization: Bearer ` → 401 → fatal.
- `llm.py:2221` and others — quarantine/timeout/agentic config reads bypass the build-locked
  `_routing_settings()` view (`llm.py:2753,3011,3039,3789,4004`).
- `llm.py:2194` — fallback live-catalog flag keyed on global backend, not the call's effective backend.
- `llm.py:2962-2988` — `write_files` batch partial-write on mid-batch OSError despite atomic framing.
- `llm.py:2760-2796` — CLI failure returns stub text as a normal result (self-labeling, but callers
  that don't inspect `.status` consume it).
- `model_router.py:966-973` — `_load_overrides()` returns unvalidated JSON (non-dict payload crashes routing).
- `model_router.py:1027-1032` — free-only mode still does paid `newest:` resolution (wasted fetch,
  misleading cache entry).
- `model_router.py:1085` — `_valid_free_model` terminal fallback uses unranked catalog order.
- `model_router.py:397` — `_model_token_cost` treats a missing price as 0.0 (mild paid mis-ranking).
- `skyn3t/model_router_paid_fallback.json` — dead duplicate of the `data/` copy, contents drifted; delete.
- `settings.py:89,637-638` — `vector_db_path` doesn't follow a `data_dir` override (state split across roots).
- `settings.py:639-640` — `Settings.__init__` does three `mkdir`s; unwritable path crashes config construction.
- `settings.py:281` — `github_similarity_max_repos` hard-capped at its own default (`le=8`).
- `settings.py:263-264` — no cross-field check `llm_retry_max_delay >= llm_retry_base_delay`.
- `settings.py:93` — `port` has no range validation.
- `secrets.py:146-153` — `_TOKEN_PATTERNS` misses JWT/`github_pat_`/`glpat-`/`sk_live_`/`npm_`/Slack webhook formats.
- `web/routes.py:5471-5476,6093` — dead `_MODELS_CACHE["models"]` key.
- `web/routes.py:4979-4981` — `_persist_env_vars` drops the whole batch on one invalid entry (`return` vs `continue`).
- `studio/runner.py:3173,5811` — `getattr(..., False)` gate defaults contradict Settings defaults (True).
- `config_detector.py:225` + `config_spec.py:96-110` — brief-side vs code-side client-name normalization
  can double-list the same logical key.
- `_scaffold.py:5259-5277` vs `code_agent.py:141-163` — two LLM-seam conventions (`OPENAI_*` vs
  `OPENROUTER_*`); preview passthrough can never feed the scaffold's `OPENAI_*` names.

## Checked, no issue

- Settings precedence (init > env > tuning > .env > secrets) matches its docstring; all 153 settings
  fields are referenced somewhere — no dead settings, no read-but-undefined accesses.
- Secret hygiene in the LLM path: Bearer key never logged; error logs use status/exception-type only;
  CLI stderr truncated; subprocess env scrubbed by `filter_env` with name- and value-pattern detection.
- `openrouter_enabled` kill-switch honored identically at every key-resolution call site.
- `free_only`/`no_claude` policy consistently applied to text pins, fallback lists, and router candidates
  (vision path excepted — M10).
- Deploy subprocess env: only the chosen provider's token injected; CLI output redacted.
- Subprocess lifecycle: tree-kill, cancellation, stderr draining, temp-file cleanup all correct.
- Budget ledger: cross-process locking, atomic writes, rollover, malformed-value guards solid (M11/M12
  are the only gaps).
- Error classification taxonomy (transient/model/fatal) sound; 401/403 fail fast.
- Env-var naming (`SKYN3T_` prefix) consistent across settings, routes setters, `.env` writer, and
  `.env.example` (except the dead keys in M6).
- `.env` writer rejects CR/LF/NUL smuggling and preserves comments.

## Suggested fix order

1. H1 (kimi/copilot stdin) — silent fake builds on Windows.
2. M10, M17 (cost-guard bypasses) — real money.
3. M1, M8, M3 (crash on bad tuning JSON; silent stub degradation) — operability.
4. M15, M16 (router self-heal disabled / cache poisoning) — 404 regressions.
5. M18, M19 (redaction + client-side secret scope) — security hygiene.
6. M6, M20, M21 (dashboard/config surface drift).

---

# Pass 2 — Deep-dive validation (2026-07-31, same swarm, resumed)

Each agent re-verified its findings against the **current working tree** (dirty vs HEAD — a fix
pass is partially applied, with regression tests in `tests/test_audit_api_config_fixes.py`,
`tests/test_settings_overrides.py`). Verdicts:

## Fixed in working tree (verified, tests green)

| Finding | Fix evidence |
|---|---|
| H1 kimi/copilot argv prompt | `_argv_prompt_hazard` + prompt-file handoff + loud `failed_cli_prompt_too_large` (llm.py:196-244, 2813-2833, 3866-3913) |
| M1 tuning-override crash | `_validated_tuning_overrides` per-key TypeAdapter validation, drops offenders (settings.py:45-69) |
| M3 free-string backends | `Literal[...]` for llm_backend/execution_backend/game_art_source + normalizer (settings.py:192-195, 379, 685). Note: pre-existing `.env` with a junk value now hard-fails boot — changelog-worthy |
| M8 silent stub degrade | throttled `llm.backend_degraded_to_stub` warning (llm.py:1850-1894) |
| M10 paid vision under free_only | image dropped with `action="image_dropped"` log (llm.py:2631-2639) |
| M15 router catalog gate | `auto`+`auto_allow_openrouter` arm added (model_router.py:837-841) |
| M16 cache poisoning | 120 s error TTL, keep-good-snapshot, empty-prime guard (model_router.py:132-137, 166-172, 236-240) |
| M17 cheap-tier cache bypass | consumption gate through `auto_model_allowed` + write guard + cheap offline default (model_router.py:814-820, 875-895) |
| M18 secret seeding/AuditLog | deploy+messaging tokens seeded (secrets.py:79-96); runner passes store (runner.py:292-294) |
| M19 client-side secret scope | `_must_stay_server` + `_normalize_spec` forcing; stack-correct Supabase prefixes (config_detector.py:106-128, 158-180, 236-250) |
| M20 render deploy provider | added to all three GUI maps (routes.py:4942-4971) |
| M21 GitHub token drift | canonical `resolve_github_token()` chain; explorer/ingestor use it (github_fetch.py:12-36) |
| M6 dead provider keys | partially: `has_any_llm` counts only openrouter (settings.py:726-737); docs note added |

## Confirmed, still open

- **M2** — tuning overrides read from `REPO_ROOT/data` but written to `settings.data_dir`
  (settings.py:109 vs cli/main.py:1113, cortex/bootstrap.py:95-101). With `SKYN3T_DATA_DIR` set,
  persisted tuning silently never applies while the dashboard shows it present. Fix: anchor all
  sides to one root.
- **M4** — `.env.example:103` `SKYN3T_BEST_OF_N=1` vs default 2; `.env.example:27` timeout 180 vs 300.
- **M5** — CLI child env strips provider auth keys (llm.py:280-287); env-key CLI auth silently
  degrades to stub. Nuance found: the strip also protects against an ambient `ANTHROPIC_API_KEY`
  overriding OAuth login — fix is docs + optional per-provider re-injection, not wholesale injection.
- **M7** — `require_codex_for_auto` hard-requires Codex at all unattended entrypoints
  (llm.py:633-638; routes.py:898-913, runner.py:4856-4861) even when claude/kimi are available.
  Deliberate policy per routes.py:901, but contradicts `.env.example:15-21` and `auto_cli_priority`.
- **M9** — last fallback candidate never quarantined; with exactly one fallback it is retried twice
  in one call, 2×(R+1) attempts against a known-dead model (llm.py:2366-2369). Traced with concrete
  candidate lists.
- **M11** — `BudgetExceeded` for ledger-lock failures is worse than reported: classified *transient*
  by the orchestrator (matches "timed out"), so a lock hiccup burns the full task-retry budget, then
  fails the build with a message reading like a spend-cap hit; a 15 s lock stall also downgrades the
  intent judge to heuristic-only for the process lifetime (runner.py:1083-1088).
- **M12** — `_save_ledger` `except OSError: pass` (llm.py:1335-1336): daily cap silently unenforced
  on write failure. Window: lock succeeds but rename/write fails (quota, AV file-locking).
- **M13** — `agentic_idle_timeout=0` disables the OpenRouter watchdog but means "use total timeout"
  on the CLI path — contradicting the settings docstring. New: **negative values instantly kill CLI
  builds** (`-5` is truthy → first readline times out → tree killed). No clamp on either path.
- **M14, F5, F9** — stale operator strings: "Auto requires Codex CLI…never falls back to OpenRouter"
  (llm.py:2151-2153); missing-key message omits alias/disabled state (llm.py:570-574, 2145);
  `preferred_cli` mislabels auto (llm.py:2131-2135); `account_source` mislabels alias keys (llm.py:2193).
- **M22** — `set_llm_routing` env writes gated behind `persist` despite docstring (routes.py:6262-6278).

---

# Pass 3 — Fix campaign state (2026-07-31)

All work below is in the WORKING TREE, uncommitted. Full suite: **4017 passed** at this checkpoint.

## Fixed this campaign (tested, suite green)

- Pass-1 fix order: H1, M1, M3, M6, M8, M15, M16, M17, M18, M19, M20, M21.
- Pass-2 ranked set: M2, M4, M9, M11, M12, M13, M14, M23, N1, N2, N3.
- Externally-reported confirmed set (verified by a 17-agent adversarial pass, fixed by an
  11-cluster workflow): Telegram inbound chat allowlist (fail closed), `delete_build`
  artifact-dir sharing/reclaim guard, proof-run process-tree kill + stage_debug offload +
  preview aux-container removal, deploy health gate + cancellation shield, learned-router
  free-substring/`codex-cli`-replay guards + catalog text-modality gate, RAG re-ingest
  delete-by-source + scoped Chroma fallback + logging, worktree TOCTOU descriptor
  verification + delivery copy integrity, liveness root-relative ignores, scheduler cron
  guards, Discord bot-API send fallback, tuning/prompt store cross-process locks, lessons
  unique index, CSP style-hash, sandbox host-path str-command rejection.

## Resolved as NOT bugs (deliberate, test-pinned contracts — annotated in code)

- M10 (configured `vision_model` is the spend opt-in under free_only), M22 (persist=False
  is deliberately env-free; docstring fixed), C1-sandbox (host fallback is documented
  design), H1b-websocket (token is base64url-encoded, not masked — refuted outright).
- M5, M7: left as documented policy decisions needing operator sign-off.

## The win-rate sweep — ALL FIXED (2026-07-31, two fix rounds)

A 35-agent subsystem sweep (foundry, proof/gates, cortex, brain, dispatch, settings, UI)
produced 40 findings; the top 28 were adversarially verified (**all survived**, evidence in
`docs/reports/winrate-sweep-confirmed.json`) and fixed by an 8-cluster round. The 12
lower-ranked leftovers were then verified (11 survived, 1 partial) and fixed by a final
7-cluster round. Headline fixes:

- Reviewer verdict refresh: a stale pre-repair `no_go` re-dispatches the reviewer AGENT
  against the delivered tree, so repaired builds stop shipping as failures.
- Gates respect posture: visual-liveness, ai_native, and game-quality gates route through
  `_gate_outcome` (lab records/dampens, release blocks; `game_quality_gates_verdict`
  forces blocking via `forced_blocking`).
- Best-of-N candidates are proofed with real build/tests via the settings knobs; typecheck
  failures after a passing build are advisory findings, not hard proof failures.
- Dispatch: fused single-transaction budget ledger off the event loop, shared keep-alive
  OpenRouter client, failover ladder capped (`llm_max_fallbacks`) with an opt-in call
  deadline (`llm_call_deadline_seconds`), agentic failover keeps codegen-class models,
  reasoning-timeout floors applied in the agentic loop, async catalog fetches.
- Brain: tournament solo plays graded by real build outcome (buffer + flush at verdict
  settle), fresh lessons get exploration slots, experience stubs filtered from recall,
  embedder dimension drift routed loudly to a sibling collection instead of silent
  memory-only writes.
- Cortex: candidates run everywhere (non-macOS), applied tuning persists across restarts,
  progressive escalation dedupe fixed, ratchet evaluator budget-guarded, STALE_BASE keeps
  verified candidates, stage_debug feeds real error text to the improver.
- Graders: reward-hacking no longer vetoes legitimate skipif/xfail; web_polish tolerates
  emoji; workflow_depth brief-scopes concepts and accepts real API layouts.
- Dashboard: gate findings propagate via build_summary, stale-stream banners + frozen
  animations on disconnect, catalog refresh actually forces, APPROVAL_REQUESTED events,
  stages settle on failure/cancel, WS reconnect backfill/dedup. UI dist rebuilt.

Remaining known-open (documented skips, both scope-limited): engine-level embedder
pinning in `rag_engine.py` (store-level fix landed; reusing an old-dim corpus needs the
engine pin), and the server-restart frozen-stage edge in the cockpit.
  Latent only — nothing reads those env vars at runtime; but the convention split is a trap, and
  `set_deploy_credential` shares it.
- **M23** — channel token precedence inverted: bare `TELEGRAM_BOT_TOKEN` shadows the GUI-saved
  `SKYN3T_` token for actual sends while status shows the GUI one (channels.py:73; also discord,
  slack, github_webhook). One-line fix.
- **M24** — config_ui_agent families miss `sveltekit`/`react_ts` (config_ui_agent.py:35-39).
- **F12** — key revoked mid-build → empty `Bearer ` → fatal 401 with misleading auth error
  (llm.py:1899-1906 vs 3119, 4334).
- Lows L1-L9 (llm.py) and router lows all re-confirmed; `.env.example` doc gaps (replicate token,
  asset-gen gate, deploy tokens) still open.

## New findings from the deep-dive (adjacent same-class)

- **N1 (med)** — `scrub_text` never applies `_URL_CRED` and `db_url` is not seeded into
  SecretsStore: a credentialed DSN (`postgres://u:p@host/db`) passes scrubbing verbatim
  (secrets.py:199-201). Also store-less `scrub_text` sites remain regex-only: integrations/service.py:102
  (egress to Telegram/Discord/Slack), github_fetch.py:84, preview_supervisor.py:282,
  app_runner.py:462/465/615; SandboxRunner at proof_run.py:1176 has `secrets=None`.
- **N2 (med)** — `_must_stay_server` trusts the LLM's `kind` and ignores name markers:
  `VITE_APP_SECRET_KEY` with `kind="api_key"` ships browser-visible (config_detector.py:117-128).
  Fix: force server for names containing SECRET/PASSWORD/PRIVATE.
- **N3 (med)** — `llm.py:345` `_WEB_UI_STACKS` missing `vue`/`sveltekit`/`react_ts`: agentic codegen
  for those stacks silently loses the entire web engineering prompt body (same drift class as M24).
- **N4 (med-low)** — `_load_overrides()` unvalidated (model_router.py:1031-1038): a corrupt
  `data/model_tier_overrides.json` crashes every `resolve()` (AttributeError/TypeError traced).
- **N5 (low)** — learned router free check `m.endswith(":free") or "free" in m`
  (intelligence/routing_recommendations.py:149) is looser than `is_free_model_id` — a paid id
  containing "free" passes `free_only`; learned `:free` picks also bypass `_valid_free_model` self-heal.
- **N6 (low)** — GH_TOKEN alias missing in github_research.py:49-54, cortex/bootstrap.py:511-513,
  routes.py:5040-5042 (route all through `resolve_github_token`).
- **N7 (low)** — `llm.py:1376-1377` learned-router construction failure fully silent
  (`except Exception: pass`); `llm.py:2687-2688` bad `data:` reference image silently dropped.
- **N8 (low)** — `_backoff_delay` in core/orchestrator.py:28-29 has the same post-cap jitter flaw
  as llm.py L5 (cap 5.0 → real max 7.5) — duplicated by design, fix both or document.
- **N9 (low)** — studio/visual_check.py:536-538: kimi vision judge has the same env-strip (M5) and
  unguarded argv prompt (H1-class) issues; short prompts keep it under thresholds today.
- **N10 (low)** — runner.py getattr defaults (`qa_playtest_enabled`/`game_visual_check_enabled`,
  False) contradict Settings defaults (True) — wrong fail direction on test doubles.
- **N11 (debt)** — `llm_backend` Literal (settings.py) and `KNOWN_CLI_PROVIDERS` (llm.py) are two
  hand-maintained lists; add a test asserting set equality.

## Updated fix order (remaining work)

1. **M9** — fallback double-retry / no-quarantine (burns money against dead models every call).
2. **M11/M12** — split `BudgetLedgerError` from `BudgetExceeded`; log ledger write failures.
3. **M13** — unify `agentic_idle_timeout` semantics; clamp negatives (instant build kill).
4. **M2** — anchor tuning-overrides read/write roots (silently dead feature under SKYN3T_DATA_DIR).
5. **N1/N2** — DSN redaction + name-marker server-forcing (secret hygiene).
6. **M7/M14/F5** — align the codex lock, reason strings, and `auto_cli_priority` story.
7. **N3/M24** — stack-set drift (vue/sveltekit/react_ts lose web prompt body + config UI).
8. **M23** — one-line channel-token precedence swap.
9. **M4 + .env.example gaps** — doc drift.
