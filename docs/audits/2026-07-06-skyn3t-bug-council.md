# SkyN3t Bug Council Report

Date: 2026-07-06
Scope: SkyN3t as an app factory, report-only bug audit
Branch observed: `codex/product-audit-council`

## Executive Summary

Five focused agents reviewed independent bug surfaces: build/debug flow, settings/model routing, generated-app security, budget/config/persistence, and tests/CI. I also verified the highest-signal findings locally with source inspection and targeted test runs.

The highest-priority issues are not ordinary product polish gaps. They are app-factory control bugs: untrusted generated apps can inherit host secrets, symlinks can copy host files into served previews, `free_only` can be bypassed by OpenRouter agentic codegen, and several "hard" budget/guard rails are not actually hard across bench runs, process restarts, or the real build loop.

## Priority Fix Order

1. Stop generated npm/proof/preview commands from inheriting host secrets.
2. Block symlink escapes when merging worktree artifacts back into served projects.
3. Enforce `free_only` consistently in OpenRouter agentic codegen.
4. Make daily USD/token caps persistent and stop resetting them inside bench runs.
5. Wire `BudgetGuard` so trips abort builds instead of only emitting telemetry.
6. Turn generated acceptance tests into real behavioral assertions or ensure skipped criteria fail quality gates.
7. Re-enable CI signal: enforce lint/type checks incrementally, remove stale pytest deselects, and add frontend test/build coverage.
8. Fix checkpoint/recovery so advertised crash evidence is actually saved and consumed.
9. Add full app shutdown cleanup for Cortex/background tasks, memory engines, ingestors, and other long-lived resources.
10. Fix preview/liveness/MCP/RAG cleanup so killed children are reaped and temp logs are removed.
11. Cap process-lifetime build/proposal caches in the dashboard.
12. Align Settings UI/API with actual runtime caps and routing metadata.
13. Tighten the new product-audit CLI metadata and output safety.

## Findings

### P1: Generated npm install/build runs with host secrets

Evidence:
- `skyn3t/npm_utils.py:82` builds npm env from `os.environ`.
- `skyn3t/studio/app_runner.py:337` uses that env for preview dependency install.
- `skyn3t/studio/proof_run.py:2795` uses node build env for proof install/build.
- `skyn3t/studio/app_runner.py:431` returns install failure details that can include log tails.

Risk: generated `package.json` lifecycle scripts or dependency install scripts run on the host and can read provider keys, GitHub tokens, cloud creds, or other local secrets. If a script prints them, diagnostics can surface them back through UI/API.

Recommended fix: make npm/proof envs default to `filter_env(os.environ)`, pass only explicit non-secret build variables, scrub npm output tails, and use `--ignore-scripts` unless a trusted/containerized path is selected.

### P1: Live preview passes real provider keys into untrusted generated app code

Evidence:
- `skyn3t/studio/app_runner.py:206` derives needed secret names from generated code/deps.
- `skyn3t/studio/app_runner.py:234` resolves them from host env or `SecretsStore`.
- `skyn3t/studio/app_runner.py:282` adds resolved keys to the generated app server env.
- `skyn3t/studio/app_runner.py:446` starts the generated app with that env.
- `skyn3t/studio/app_runner.py:461` returns unsanitized log tails on startup failure.

Risk: generated app code is untrusted. A generated project can reference an env var or SDK dependency, receive a real OpenRouter/provider key, and exfiltrate or print it.

Recommended fix: do not pass real provider keys directly into previews by default. Use a local mediation/proxy or mock key, require explicit per-project approval for real key passthrough, and scrub serve logs before API responses.

### P1: Worktree artifact copy follows symlinks into served project files

Evidence:
- `skyn3t/worktree.py:116` iterates files without rejecting symlinks.
- `skyn3t/worktree.py:164` copies with `shutil.copy2`, which follows symlinks by default.
- `skyn3t/web/routes.py:175` serves project files after preview-root checks.

Risk: generated code or npm scripts can create a symlink such as `public/leak.txt -> /Users/.../.env`; merge/sync copies the target contents into the project tree, where preview routes can serve it.

Recommended fix: reject symlinks in `_iter_files()` or only allow symlinks whose resolved targets stay inside the worktree. Add a regression test with a symlink to an external temp secret file.

### P1: `free_only` is bypassed by OpenRouter agentic codegen

Evidence:
- `skyn3t/adapters/llm.py:819` strips paid `model_override`/preferred model pins in ordinary `complete()`.
- `skyn3t/adapters/llm.py:1218` agentic OpenRouter codegen selects `model`, `openrouter_codegen_model`, or `preferred_model` separately and sends it to `_openrouter_agentic()`.
- `skyn3t/agents/code_agent.py:604` and `skyn3t/agents/code_agent.py:652` route Studio/codegen into `agentic_build()`.
- `skyn3t/web/routes.py:1986` previews paid pins as blocked when `free_only` is on, so UI and runtime can disagree.

Risk: an operator can believe paid models are blocked while whole-app codegen still uses a paid OpenRouter pin.

Recommended fix: extract one model-pin resolver that applies `free_only`/`no_claude` and use it from both `complete()` and `agentic_build()`. Add tests for `model_override`, `openrouter_codegen_model`, and `preferred_model` under `free_only=True`.

### P1: Bench resets daily spend/token counters, so daily caps are not hard there

Evidence:
- `skyn3t/cli/main.py:915` says bench resets cumulative spend before each case.
- `skyn3t/cli/main.py:935` zeros `spent_day` and `tokens_day`.
- `skyn3t/adapters/llm.py:461` enforces daily USD/token caps from those same counters.

Risk: a multi-case bench or ratchet can exceed the configured daily USD/token cap while each case appears isolated.

Recommended fix: reset only `spent_build`. Compute per-case bench cost from call-ledger deltas while leaving daily counters cumulative. Add a fake-budget bench test proving case 2 trips a daily cap reached by case 1.

### P1: Daily USD/token caps reset on process restart

Evidence:
- `skyn3t/adapters/llm.py:503` creates a fresh `BudgetTracker` per `LLMClient`.
- `skyn3t/adapters/llm.py:449` initializes daily counters to zero.
- `skyn3t/observability/cost_tracker.py:7` documents cost tracking as in-memory only.

Risk: `daily_usd_cap` and `daily_token_cap` are not truly daily across CLI/web restarts.

Recommended fix: persist a dated spend ledger under `settings.data_dir`, load it in `BudgetTracker`, update it from `record()`, and roll it over by local date. Test with two `LLMClient` instances sharing a temp `data_dir`.

### P1: `BudgetGuard` trips are telemetry-only in the real build loop

Evidence:
- `skyn3t/self_healing/budget.py:116` raises `GuardTripped` from `check()`.
- `skyn3t/self_healing/budget.py:83` catches trip exceptions in event handlers and only announces them.
- `skyn3t/self_healing/budget.py:157` `watchdog()` catches and returns.
- `skyn3t/studio/runner.py:2967` calls only `reset`/`heartbeat`.
- `skyn3t/studio/runner.py:499` `_obs_call` swallows all optional collaborator exceptions.

Risk: loop/stall guardrails can pass unit tests but fail to abort runaway real builds.

Recommended fix: attach the guard to `event_bus`, lifecycle-manage the watchdog, call `check()` at stage/fix-loop boundaries outside `_obs_call`, and translate `GuardTripped` into failed/no-go build status. Add an integration test proving a tripped guard stops later stages.

### P1: CI can go green while high-signal checks are failing or absent

Evidence:
- `.github/workflows/ci.yml:40` runs ruff with `|| true`.
- `.github/workflows/ci.yml:46` runs mypy with `|| true`.
- `.github/workflows/ci.yml:57` deselects three proof-regression tests.
- Those deselected tests passed locally in the agent run: `3 passed, 9 warnings in 6.13s`.

Risk: CI status can claim health while lint/type regressions or proof regressions are hidden.

Recommended fix: remove stale deselects if the local pass holds in CI, make ruff/mypy blocking at least for changed files or a ratcheted baseline, and run the frontend build in CI.

### P1: Generated acceptance tests document behavior but skip every behavioral check

Evidence:
- `skyn3t/agents/test_author.py:66` describes generated tests as acceptance coverage.
- `skyn3t/agents/test_author.py:125` emits `@pytest.mark.skip` for every acceptance criterion.
- `skyn3t/agents/build_verifier.py:122` flags skipped/xfailed tests only when the file has no assertion tokens, so structural asserts can mask skipped behavioral criteria.

Risk: a generated app can look test-first while all meaningful behavioral acceptance checks are skipped.

Recommended fix: either synthesize executable behavioral assertions for supported criteria, or make skipped criteria count as a quality/verifier gap even when structural assertions exist.

### P2: Checkpoint/recovery is advertised but build state is not saved or consumed

Evidence:
- `skyn3t/persistence/checkpoint.py:1` describes EventBus/build-state checkpoints.
- `skyn3t/persistence/recovery.py:93` returns `files_state`/`task_state`.
- `skyn3t/cli/main.py:1389` calls restore but discards result.
- Repo search found no production `CheckpointManager.save(...)` call outside tests.

Risk: crash recovery and post-failure evidence are unreliable despite the API existing.

Recommended fix: add periodic checkpoint writes in build lifecycle, and either consume restored state for resume or rename the behavior as event-history restore. Add an integration test for visible/resumed state after restart.

### P2: Codegen CLI provider with empty model is rendered as `auto`

Evidence:
- `skyn3t/studio/runner.py:350` probes CLI provider and returns `codegen_cli_model`.
- `skyn3t/studio/runner.py:359` can return an empty string when provider is available but no model is pinned.
- `skyn3t/studio/runner.py:2898` persists that as `codegen_model`.
- `skyn3t/web/ui/src/routes/Studio.jsx:386` renders empty codegen model as `auto`.

Risk: the UI loses which provider actually authored codegen. This makes model-routing debugging harder and can mislead the operator.

Recommended fix: persist a stable label such as `claude-cli:default`, or add `codegen_backend/provider` to `model_trace` and render that.

### P2: `/llm/routing` accepts arbitrary codegen CLI provider values

Evidence:
- `skyn3t/web/routes.py:2320` normalizes any string into `codegen_cli_provider`.
- `skyn3t/agents/code_agent.py:1062` treats any available executable as a codegen CLI provider.
- `skyn3t/adapters/llm.py:1247` falls back to `[provider, "-p", prompt]` for unknown providers.

Risk: the API contract is wider than the runtime model and can execute unexpected local commands if configured.

Recommended fix: allowlist `claude`, `kimi`, `copilot`, and empty string in `set_llm_routing`; return 422 for anything else.

### P2: Product-audit `--llm` metadata is wired but unused

Evidence:
- `skyn3t/cli/main.py:1106` builds an `LLMClient` when `--llm` is passed.
- `skyn3t/audit/agents.py:31` stores `llm`/`use_llm` on `AuditContext`.
- `skyn3t/audit/render.py:27` reports `LLM-assisted`.
- No audit agent reads `ctx.llm` or `ctx.use_llm`.

Risk: audit reports can claim LLM assistance when no LLM-assisted analysis happened.

Recommended fix: remove the flag/metadata for V1 or add a bounded LLM synthesis step with a fake-LLM test proving invocation.

### P2: Product-audit repo root/output behavior can audit or write the wrong tree

Evidence:
- `skyn3t/cli/main.py:1098` uses `Path.cwd()` as `repo_root`.
- `skyn3t/cli/main.py:1099` defaults Markdown under that cwd.
- `skyn3t/audit/render.py:77` writes any supplied output paths after creating parents.

Risk: running from a subdirectory or generated project can audit the wrong tree. Explicit output paths can write outside repo/data unintentionally.

Recommended fix: anchor audits to the package/repo root, resolve outputs, and require default outputs under repo `docs/audits` and `settings.data_dir/audits` unless an explicit escape hatch is added.

### P2: Settings/Studio UI regression coverage is mostly static source checks

Evidence:
- `skyn3t/web/ui/package.json:7` has no `test` script.
- `tests/test_web_ui.py:239` and `tests/test_web_ui.py:261` mostly assert source strings for Settings/Studio behavior.
- Agent run: `npm test -- --run` failed with `Missing script: "test"`.

Risk: UI/API mismatches, stale query state, or broken controls can pass Python tests.

Recommended fix: add a small frontend test runner or Playwright smoke that exercises Settings mutations and Studio model/cost rendering. At minimum, add `npm run build` to CI.

### P2: Proof isolation weakens when Docker is absent

Evidence:
- `skyn3t/security/sandbox.py:167` falls back from Docker to subprocess.
- `skyn3t/security/sandbox.py:225` warns that host subprocess fallback cannot enforce network isolation.

Risk: generated proof commands may run with host network access even when callers request `network=False`.

Recommended fix: make hermetic proof tests require Docker/container backend in CI, or explicitly mark host-subprocess fallback as degraded and prevent it from satisfying hermetic proof claims.

### P2: Learning-loop lessons are captured stage-agnostic while injection is stage-specific

Evidence:
- `skyn3t/studio/runner.py:469` injects lessons by stage.
- `skyn3t/studio/runner.py:2586` records a `stages` list but not a singular `stage`.
- `skyn3t/intelligence/learning_loop.py:293` falls back to `stage=""`.

Risk: concrete proof/review/gate lessons can be injected broadly into unrelated stages.

Recommended fix: emit category-specific lesson captures with `stage="proof"`, `stage="reviewer"`, `stage="<gate>"`, etc., while keeping one build-level lesson at `stage=""`.

### P3: `daily_token_cap` and autonomous build cap are missing from web settings contract

Evidence:
- `skyn3t/config/settings.py:234` defines `daily_token_cap`.
- `skyn3t/config/settings.py:235` defines `autonomous_daily_build_cap`.
- `skyn3t/web/deps.py:385` exposes `daily_token_cap` in budget snapshot.
- `skyn3t/web/routes.py:2463` omits both from `/settings`.
- `skyn3t/web/ui/src/routes/Settings.jsx:373` omits both from runtime flags.
- `skyn3t/web/ui/src/routes/Settings.jsx:1047` labels the runtime panel read-only.

Risk: UI/API/runtime contracts drift; operators can see some caps but not all real caps that can stop builds.

Recommended fix: include all factory caps in `settings_payload`, render them clearly, and add update endpoints if the product promise is that caps are editable from Settings.

### P3: New product-audit report can persist absolute local paths

Evidence:
- `skyn3t/audit/runner.py:78` stores `repo_root: str(root)`.
- `skyn3t/audit/render.py:21` renders the repo root path.
- `skyn3t/cli/main.py:1086` defaults report output into repo docs.

Risk: default report files can leak `/Users/...` paths into committed Markdown.

Recommended fix: render repo root as `.` or a redacted basename by default, with an explicit `--include-local-paths` flag if absolute paths are needed.

### P3: Restored EventBus metrics can contradict restored event history

Evidence:
- `skyn3t/core/events.py:199` snapshots `published` and bounded `history`, but not `_type_counts`.
- `skyn3t/core/events.py:206` restores type counts only from bounded history.
- `skyn3t/web/routes.py:1300` describes per-type counts as monotonic.

Risk: after restore, total events can exceed sum of per-type counts if history was truncated, weakening audit evidence.

Recommended fix: include `type_counts` in snapshots and restore it when present, falling back to history reconstruction for old snapshots.

## Code Quality, Optimization, And Leak Addendum

This second pass focused on maintainability, dead code, memory/process leaks, import cycles, lint hygiene, and oversized hotspots. It found one additional P1 and several P2 cleanup issues. It did not find a large source-code TODO/FIXME cluster; most TODO-like hits are test fixtures or anti-placeholder scanners.

### P1: Web shutdown leaves long-lived runtime resources unmanaged

Evidence:
- `skyn3t/web/app.py:141` shutdown only calls `state.stop_all_serves()` and `hub.close()`.
- `skyn3t/cli/main.py:1424` can start Cortex when autonomous learning/builds are enabled.
- `skyn3t/cortex/bootstrap.py:222` stores live `asyncio.create_task()` handles.
- `skyn3t/memory/store.py:109` exposes `MemoryStore.close()` for the SQLAlchemy engine.
- `skyn3t/cli/main.py:681` starts an `ExperienceIngestor` and drops the reference.

Risk: dev-server restarts, tests, or graceful shutdown can leave async tasks, subscriptions, DB pools, and ingestor hooks alive until process teardown.

Recommended fix: add async `AppState.close()` that stops serves, closes the websocket hub, awaits `cortex.stop()`, stops retained ingestors, and closes memory/vector resources. Call it from FastAPI shutdown. Add a lifecycle test with fake cortex/memory/ingestor objects.

### P2: General liveness loop stops previews but does not call serve cleanup

Evidence:
- `skyn3t/studio/liveness.py:317` starts a preview each round.
- `skyn3t/studio/liveness.py:326` finally calls only `app_runner.stop(app)`.
- `skyn3t/studio/app_runner.py:506` documents that `cleanup_serve()` is required to reap zombies and remove temp logs.

Risk: repeated liveness checks can accumulate zombie children and `skyn3t-serve-*.log` temp files, especially on failed preview boots.

Recommended fix: call `cleanup_serve(app)` in liveness finally, mirroring visual/playtest cleanup paths. Add a regression with a failed preview carrying `pid` and `log_path`.

### P2: MCP/RAG/workflow gate shutdown can leave zombies after forced kill

Evidence:
- `skyn3t/studio/mcp_check.py:520` terminates, waits, then kills without a second wait.
- `skyn3t/studio/rag_check.py:658` has the same pattern.
- `skyn3t/studio/workflow_check.py:33` reuses RAG shutdown.

Risk: generated servers that ignore SIGTERM can be SIGKILLed but not reaped until process exit.

Recommended fix: after `proc.kill()`, always call bounded `proc.wait(timeout=...)`. Test with a fixture child that traps/ignores SIGTERM.

### P2: Live build/proposal caches are unbounded

Evidence:
- `skyn3t/web/deps.py:180` owns process-lifetime `builds` and `proposals` dictionaries.
- `skyn3t/web/routes.py:379` appends build records on submit.
- `skyn3t/web/deps.py:257` replays build events into memory.
- `skyn3t/web/routes.py:588` slices API responses but not stored state.
- `skyn3t/web/routes.py:2129` has only manual proposal clearing.

Risk: a long-running dashboard doing many builds/proposals grows heap indefinitely despite persisted history existing.

Recommended fix: cap terminal builds/proposals by count or TTL while pinning active records; rely on SQLite/persistent history for older entries. Add a test that more than N terminal events keeps cache size under N.

### P2: Ruff is failing, so lint hygiene is not enforceable yet

Evidence:
- Local `ruff check skyn3t tests --statistics` reported 44 issues.
- Main production examples include `skyn3t/agents/game_designer.py:19` unused import, `skyn3t/agents/_scaffold.py:3220` redundant outer f-string, and `skyn3t/studio/mcp_check.py:196`/`:211` missing exception chaining.

Risk: small stale-code patterns remain normalized, and CI cannot become a trusted quality gate until the baseline is clean or ratcheted.

Recommended fix: run safe `ruff --fix` for imports/formatting, manually review the `B904` exception chaining and generated-code string case, then enforce ruff on changed files or a baseline.

### P2: Import cycles increase initialization and refactor risk

Evidence from the static AST scan:
- A large cycle crosses `llm -> proof_run -> qa/visual/assets -> agents -> llm`, including `skyn3t/adapters/llm.py:249`, `skyn3t/studio/qa_playtest.py:39`, `skyn3t/studio/asset_foundry.py:19`, and `skyn3t/agents/game_designer.py:165`.
- A second cycle exists in integrations: `skyn3t/integrations/channels.py:258` imports providers that import channel base types such as `skyn3t/integrations/discord.py:19`.

Risk: import-time behavior gets fragile, optional dependency degradation becomes harder to reason about, and future module splits can trigger circular import failures.

Recommended fix: move shared types/protocols to leaf modules, invert provider registration in integrations, and keep LLM client utilities independent of proof/agent imports.

### P2: Hotspot files and functions are too large for safe change velocity

Evidence:
- `skyn3t/studio/runner.py` is 4,223 lines.
- `skyn3t/web/routes.py` is 3,054 lines.
- `skyn3t/studio/proof_run.py` is 2,979 lines.
- Static scan found `StudioRunner.start` at `skyn3t/studio/runner.py:2825` is about 898 lines, and `build_router` at `skyn3t/web/routes.py:2476` is about 579 lines.
- Broad exception density is high: `runner.py` 77 broad catches, `routes.py` 50, `cli/main.py` 53.

Risk: future bug fixes are harder to localize, and swallowed exceptions can hide regressions until they surface as poor generated apps.

Recommended fix: split by behavior, not style-only refactors: move build lifecycle phases out of `start`, split route groups into routers, and replace broad catches in core paths with typed exceptions plus explicit degrade events.

### P2: Product-audit implementation is too shallow for real factory-quality grading

Evidence:
- `skyn3t/audit/agents.py:178` defines product dimensions as path tuples.
- `skyn3t/audit/agents.py:190` scores by file existence.
- `skyn3t/audit/agents.py:214` uses roadmap phrase matches.
- `skyn3t/audit/agents.py:295` repeats existence checks for comparator scoring.

Risk: present-but-broken subsystems still score well, so the audit can miss the exact kind of bug council findings this report surfaced.

Recommended fix: replace existence scoring with contract probes: `default_pipeline()` stage shape, proof/liveness gates, build summary fields, lesson counts, web route payloads, model routing traces, and generated-app smoke outcomes.

### P3: `studio serve` CLI leaks temp logs on normal stop/failure

Evidence:
- `skyn3t/cli/main.py:1188` starts `AppRunner`.
- `skyn3t/cli/main.py:1192` exits on failed start without cleanup.
- `skyn3t/cli/main.py:1200` stops on Ctrl-C but does not call `cleanup_serve(app)`.

Risk: repeated manual serve sessions can leave temp logs and possibly unreaped children.

Recommended fix: wrap serve lifecycle in `finally: cleanup_serve(app)`.

### P3: Agentic CLI stderr drain task is cancelled but not awaited

Evidence:
- `skyn3t/adapters/llm.py:1336` creates `err_task`.
- `skyn3t/adapters/llm.py:1368` cancels it in `finally` but does not await it.

Risk: repeated agentic builds can leave pending cancellation work or "task destroyed but pending" noise during loop teardown.

Recommended fix: after cancel, `await err_task` under `contextlib.suppress(asyncio.CancelledError)`.

### P3: Frontend has build coverage but no test/lint hygiene

Evidence:
- `skyn3t/web/ui/package.json:7` only defines `dev`, `build`, and `preview`.
- `npm test -- --run` failed with `Missing script: "test"`.
- No ESLint/Vitest/Jest/Playwright config was found under `skyn3t/web/ui`.

Risk: UI behavior can regress even when Python tests and Vite build pass.

Recommended fix: add at least a minimal frontend lint/test script, plus Playwright smoke for Settings and Studio controls.

### P3: Local-only frontend exports may be dead public API

Evidence:
- `skyn3t/web/ui/src/components/cockpit.jsx:5` exports `debugRowsFromEvents`, `latestSnapshot`, `latestRunningSlug`, and `stageTone`, but static scan found no repo-local refs outside the defining file.

Risk: low. These may be intended test-facing helpers, but otherwise they make the component API noisier than needed.

Recommended fix: de-export them unless tests or external callers intentionally import them.

## Verification Performed

Local verification in this coordinating pass:

- `pytest tests/test_bughunt_batch1.py tests/test_bughunt_batch2.py tests/test_bughunt_batch3.py tests/test_bughunt_batch4.py tests/test_bughunt_batch5.py -q` -> `31 passed in 0.79s`.
- `pytest tests/test_product_audit.py tests/test_web_llm_config.py tests/test_serverside_llm_routing.py tests/test_routing_readiness.py tests/test_config_surfacing.py -q` -> `76 passed in 7.41s`.
- `ruff check skyn3t tests --statistics` -> failed with 44 findings.
- Static resource/dead-code sweeps covered subprocess cleanup, background tasks, large files/functions, broad catches, TODO/FIXME clusters, and frontend scripts.

Agent-reported verification:

- Settings/model-routing agent: targeted coverage -> `26 passed in 1.36s`.
- Budget/config/persistence agent: targeted guard/product-audit tests -> `7 passed`.
- Build-debugger agent: targeted regression checks -> `6 passed in 0.19s`.
- Test/CI agent:
  - `python3 -m pytest --collect-only -q` -> `2315 tests collected`.
  - focused suite -> `40 passed in 10.12s`.
  - CI-deselected tests only -> `3 passed, 9 warnings in 6.13s`.
  - CI-shaped pytest command -> `2309 passed, 3 skipped, 3 deselected, 67 warnings in 279.93s`.
  - `npm run build` in `skyn3t/web/ui` -> passed.
  - `npm test -- --run` in `skyn3t/web/ui` -> failed: missing `test` script.

No intentional code fixes were made as part of this report.
