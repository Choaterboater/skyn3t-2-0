# Swarm Debug Findings — 2026-06-21

19 confirmed bugs (22 candidates, adversarially verified) from a 6-lens swarm debug of SkyN3t. Class: dead-wired params, silent fallbacks, brief-blind scoring, vocab/route mismatches.

STATUS: #3 (cli->react) FIXED (merge 58cd05d). Rest open.

## [HIGH] Learned model router never receives task_type — per-task-type routing is dead-wired
- **Where:** skyn3t/adapters/llm.py:176 (call) vs skyn3t/intelligence/routing_recommendations.py:97-114, :49-72
- **What:** LLMClient.complete() is the only place that resolves a model: `model = self.router.resolve(tier, file_hint)`. It passes only (tier, file_hint) and complete() has no task_type parameter at all. But the learned routing subsystem is built entirely around a task_type dimension: LearnedModelRouter.resolve(tier, file_hint, task_type) -> RoutingRecommender.recommend(tier, task_type) -> tournament.bucket_key(tier, task_type) keyed as `<tier>:<task_type>`. Because the serving path always uses the default
- **Fix:** Thread task_type end-to-end so the serving lookup matches the recorded buckets:

1) skyn3t/adapters/llm.py: add `task_type: str = ""` to `complete()`'s keyword args (after `json_mode`), and change line 176 to `model = self.router.resolve(tier, file_hint, task_type=task_type)`. Since base `ModelRouter.resolve` (core/model_router.py) does not accept task_type, either add an ignored `**_` / `task_type=""` param there, or feature-detect (e.g. only pass task_type when `isinstance(self.router, Learned

## [HIGH] Architect's file-level plan is dead-wired: CodeAgent never receives it (reads planner BuildPlan, not prior['architect'])
- **Where:** skyn3t/studio/runner.py:646-656 (_base_payload) + skyn3t/agents/code_agent.py:90-94,182-193,249-257 + skyn3t/agents/architect.py:78-87
- **What:** The architect stage runs before the code stage and produces a real file decomposition: its TaskResult.output is {"plan": {"stack","summary","files":[{"path","purpose"}...],"build_order"}, "stack":...} (architect.py:78-87), stored by the runner in prior['architect']. But the code stage's payload['plan'] is set by _base_payload to plan.to_dict() — the PLANNER's BuildPlan (runner.py:652). BuildPlan.to_dict() (planner.py:107-117) emits only slug/brief/stack/stages/checklist/test_first/best_of_n/note
- **Fix:** In CodeAgent.execute (skyn3t/agents/code_agent.py:88-90), prefer the architect's plan from prior, falling back to the planner plan:

    p = task.payload
    brief = p.get("brief", "") or p.get("slug", "app")
    prior = p.get("prior", {}) if isinstance(p.get("prior"), dict) else {}
    arch = prior.get("architect", {}) if isinstance(prior.get("architect"), dict) else {}
    arch_plan = arch.get("plan") if isinstance(arch.get("plan"), dict) else {}
    base_plan = p.get("plan") if isinstance(p.g

## [HIGH] Keyword stack fallback maps a CLI/command-line brief to React, never python_cli
- **Where:** skyn3t/studio/stack_selector.py:35-50,14-21 + skyn3t/studio/planner.py:38,44
- **What:** When the LLM selector is unavailable (stub backend / no API key — exactly the offline path the code base supports), select_stack falls back to keyword_choice (stack_selector.py:96). keyword_choice calls the planner's detect_stack, which can return 'cli' for a brief containing 'command line tool'/'terminal tool' (planner.py:38). _to_real_builder then collapses it: 'cli' is NOT in REAL_BUILDER_STACKS and NOT in _COLLAPSE, so it returns the hard-coded default 'react' (stack_selector.py:38). The cho
- **Fix:** Map the planner's "cli" vocabulary to the python builder in stack_selector.py so the collapse no longer silently defaults to react.

Minimal fix (stack_selector.py:24):
  _COLLAPSE = {"nextjs": "react", "flask": "fastapi", "django": "fastapi", "cli": "python"}

This makes _to_real_builder("cli") collapse to "python" (a real builder), so keyword_choice for a CLI brief yields StackChoice(stack="python"); planner.plan(stack_hint="python") -> plan.stack="python" -> python checklist (README.md, pypro

## [HIGH] Final verdict gate is brief-blind: the intent-aware LLM review is overwritten by a purely structural rescore
- **Where:** skyn3t/studio/runner.py:885-889, 912-924; skyn3t/agents/reviewer.py:27-63
- **What:** The FINAL build verdict is decided by `_rescore_delivered()` (runner.py:288-307), which calls `reviewer.heuristic_score()`. That heuristic is 100% structural: 25 pts for an entrypoint NAME existing, 35 pts for >=5 non-empty source files, 20 pts for a manifest, 20 pts for parseable build config. It NEVER reads the brief — `payload` is passed but `heuristic_score` ignores it. The only brief-aware signal in the whole reviewer (`_maybe_llm_score`, reviewer.py:135-157, which prompts 'completeness and
- **Fix:** Make the final gate intent-aware instead of letting the brief-blind rescore be the sole authority. Two complementary changes in runner.py:

(A) Do not let the brief-blind rescore OVERWRITE a brief-aware no_go — only let it RECOVER a stale no_go after a successful fix loop, and AND the two verdicts when the stage verdict was informative:

  stage_verdict = verdict  # captured at line 828 (may be brief-aware)
  re_verdict, re_score, re_gaps = self._rescore_delivered(project_dir)
  reviewer_score =

## [HIGH] _honest_score only halves a failed-proof score; the verdict still derives 'go' from a brief-blind rescore, so a high score can co-exist with a real intent failure
- **Where:** skyn3t/studio/runner.py:891-896, 1034-1039
- **What:** `_honest_score` multiplies the blended score by 0.5 when proof failed — a reasonable penalty for build/boot failure. But there is no analogous honesty adjustment for INTENT mismatch: `final_score = 0.6*reviewer_score + 0.4*proof.score`, where `reviewer_score` was set to `max(reviewer_score, re_score)` (line 887) — i.e. it ratchets UP to the brief-blind structural rescore and never down for a brief mismatch. A correctly-built-but-wrong-thing deliverable (proof passes because it boots) gets full s
- **Fix:** Introduce a brief/intent-correctness term into the honest score and gate, and stop discarding the brief-aware signal:

1) Do not let the brief-blind structural rescore overwrite a lower brief-aware reviewer verdict. Replace runner.py:887-889 so the structural rescore can only RESCUE a stale-broken verdict for proof purposes, not inflate a brief-mismatch:
   - Keep a separate `structural_score = re_score` for the proof/recovery path, but compute `intent_score` from a brief-aware check and require

## [HIGH] LearnedModelRouter never receives task_type, so the learned router can essentially never return a recommendation
- **Where:** skyn3t/adapters/llm.py:176 (resolve call) vs skyn3t/intelligence/routing_recommendations.py:97-114 (resolve signature) and :53-59 (bucket lookup)
- **What:** `LearnedModelRouter.resolve(self, tier, file_hint=None, task_type='')` is called by the only LLM entry point as `self.router.resolve(tier, file_hint)` (llm.py:176). `file_hint` is passed POSITIONALLY into the second parameter (`file_hint`), so `task_type` is ALWAYS the default `''`. Therefore `recommend()` always computes `bucket_key(tier, '')` (the tier-only bucket). The 'fallback' at routing_recommendations.py:56-59 then recomputes `bucket_key(tier, '')` again — the exact same empty bucket — s
- **Fix:** Two-part fix (both needed for the knob to actually fire):

(A) Thread task_type to resolve. Add a `task_type: str = ""` kwarg to `LLMClient.complete` (llm.py:166-175) and pass it through: `model = self.router.resolve(tier, file_hint=file_hint, task_type=task_type)` (llm.py:176). Base `ModelRouter.resolve` ignores the extra kwarg or should accept/discard it; LearnedModelRouter already accepts it. Have build-pipeline call sites pass a meaningful task_type per stage (e.g. "scaffold", "code", "revie

## [HIGH] Approving/rejecting a persisted (zombie) build returns 404 — approve_build only checks in-memory state.builds
- **Where:** skyn3t/web/routes.py:249-252 (approve_build) + 887-897 (/studio/approve route); list_builds at routes.py:163-179; SPA Studio.jsx:107-112,261-292
- **What:** approve_build does `rec = state.builds.get(build_id); if rec is None: raise KeyError(build_id)`, and the /studio/approve route maps KeyError -> HTTP 404 'build not found'. But list_builds (routes.py:170-176) augments the live cache with persisted rows from `state.memory.recent_builds()` (memory/store.py:106-116) — those dict rows carry a `build_id` and a `status` (e.g. 'running' for a build that was interrupted when the server was last killed) but are NEVER inserted into `state.builds`. The Stud
- **Fix:** Make approve_build tolerate persisted-but-not-cached builds instead of 404-ing, mirroring decide_proposal's tolerance of unseen ids.

In approve_build (routes.py:249), when state.builds has no record, fall back to the persisted row and update status in the store rather than raising KeyError:

```python
async def approve_build(state, build_id, approved=True, reason=""):
    rec = state.builds.get(build_id)
    status = "approved" if approved else "rejected"
    if rec is not None:
        rec.sta

## [MEDIUM] code_agent ignores agentic_build's ok flag — a failed CLI agent's partial output ships with no degradation signal
- **Where:** skyn3t/agents/code_agent.py:108-126 (and :148-157)
- **What:** agentic_build() returns {ok, backend, error} and explicitly signals failure: ok=False on nonzero exit, timeout-to-stub, or exception (adapters/llm.py:278-282, :274-277). In code_agent.execute the result is stored only as metadata (`self.metadata["agentic"] = res`) and is NEVER inspected. The choice between shipping the agent's files and falling back to the scaffold is made purely on disk byte count: `if disk and code_bytes >= 800: files = disk`. So if the agentic CLI crashed/aborted (ok=False) b
- **Fix:** In code_agent.execute, read the `ok`/`error` fields that agentic_build already returns and propagate them, instead of only storing metadata.

Concretely (code_agent.py ~114-126 and ~148-157):

  res = await self.llm.agentic_build(...)
  self.metadata["agentic"] = res
  agent_ok = bool(res.get("ok"))
  disk = self._read_files(worktree)
  code_bytes = sum(...)
  if disk and code_bytes >= 800:
      files = disk
      agentic_degraded = not agent_ok      # adopted partial output from a FAILED sessi

## [MEDIUM] ContractVerifier degrades to the planner checklist because the architect file plan never reaches its payload
- **Where:** skyn3t/agents/contract_verifier.py:25-40,103-106 + skyn3t/studio/runner.py:652
- **What:** extract_planned_files looks for payload['plan']['files'] (the architect's file-level plan) and verifies every declared file exists and is non-empty — this is the documented 2.0 'File-level plan as verifier checklist'. But because the verifier stage also gets payload['plan'] = plan.to_dict() (planner BuildPlan, no 'files' key), the lookup loop at contract_verifier.py:34 falls through 'files','file_plan','planned_files' and lands on 'checklist'. So the contract verifier silently checks the planner
- **Fix:** Feed the architect's file plan into the verifier, preferring it over the planner checklist. Two-part fix:

(A) In skyn3t/agents/contract_verifier.py `extract_planned_files`, look in `prior["architect"]["plan"]["files"]` BEFORE falling back to the planner-dict checklist. E.g. at the top of the function:

```python
prior = payload.get("prior")
if isinstance(prior, dict):
    arch = prior.get("architect") or prior.get("architecture") or {}
    arch_plan = arch.get("plan") if isinstance(arch, dict)

## [MEDIUM] Proof entrypoint/boot check is stack-agnostic, so a brief's required stack is never verified against the delivered tree
- **Where:** skyn3t/studio/proof_run.py:162-176, 229-248
- **What:** proof_run's behavior gate (the 'no runnable entrypoint => not proven' rule) calls `_entrypoint_check` which uses `vc.find_entrypoints` against a fixed ENTRYPOINT_NAMES set that mixes all stacks (main.py, cli.py, index.html, App.tsx, server.js...). It only checks that SOME entrypoint name exists and (for python) that it imports. It never checks that the entrypoint TYPE matches `stack`/the brief intent. A 'website'/'static'/'nextjs' brief delivered as a python `main.py` passes the entrypoint gate
- **Fix:** Add an explicit stack-kind assertion to proof_run rather than relying on the loose checklist half-gate. In `_entrypoint_check` (or a new `_stack_shape_ok`), once an expected stack family is known, require the delivered tree to contain the family's signature entrypoint/manifest and fail the proof otherwise:\n\n- Map stack -> required shape: web/static/react/nextjs -> must contain index.html (or app/page.tsx for nextjs) AND package.json for the node web stacks; python/cli/fastapi/flask/django -> m

## [MEDIUM] SelfTuningEngine writes agent-config knobs that NO agent or pipeline stage ever reads — the entire tuning loop is recorded-but-never-consumed
- **Where:** skyn3t/memory/tuner.py:28-36 (SAFE_KNOBS/SAFE_FLAGS) + skyn3t/memory/tuner.py:176-197 (cfg[key]=new); consumers absent across skyn3t/agents/* and skyn3t/core/*
- **What:** The tuner reacts to reflection/insight events by mutating live agent `.config` dicts, but it can only write the keys in SAFE_KNOBS={temperature,max_retries,best_of_n,max_tokens,timeout_s} and SAFE_FLAGS={critic_enabled,reflective_retry,json_mode}. A grep of the whole tree shows NONE of these keys are ever read back from an agent's `self.config`. The only agent-config keys actually consumed are `timeout_ms`, `headless` (browser_agent.py:64-65) and `allow_remote_deploy` (deploy_agent.py:128) — non
- **Fix:** Two-part fix. (1) Make `temperature` actually consumed or stop emitting it: either thread an agent/Settings temperature into the LLM/claude_cli call sites (so `lower_temperature` insights take effect), or remove the `lower_temperature` branch from `_insight_to_suggestion` (tuner.py:122-123) and the `temperature` entry from SAFE_KNOBS so the meta_agent's most common suggestion (meta_agent.py:127) doesn't translate into a guaranteed dead write. (2) Either (a) make the tuner write to the persistenc

## [MEDIUM] MetaAgent build-quality hypotheses treat unscored builds (default score 0.0) as scored, biasing the signal
- **Where:** skyn3t/memory/meta_agent.py:136-139 + skyn3t/memory/models.py:83 + skyn3t/memory/store.py:111-115
- **What:** `_build_quality_hypotheses` filters `scored = [b for b in builds if b.get('score') is not None]` (meta_agent.py:136), intending to drop builds that were never scored. But `BuildRow.score` is declared `Mapped[float] = mapped_column(Float, default=0.0)` (models.py:83) and `recent_builds` returns that raw value (store.py:113). A build that was persisted before scoring (or that errored out) therefore has score=0.0, not None, so it passes the `is not None` filter and is counted as a real 0/100 sample
- **Fix:** Make "unscored" representable distinctly from "scored 0". Three coordinated changes:

1. models.py:83 — make the column nullable with no default: `score: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)` so a build persisted before scoring is genuinely NULL.

2. studio/manifest.py:55 — change `score: float = 0.0` to `score: float | None = None`, and only set it to a real number at runner.py:897 (`manifest.score = final_score`). Audit the few `manifest.score`/`(manifest.sc

## [MEDIUM] DB-only builds are visible in list_builds but un-approvable (KeyError -> 404)
- **Where:** skyn3t/web/routes.py:249-252 (approve_build) vs skyn3t/web/routes.py:163-179 (list_builds) and skyn3t/web/routes.py:887-897 (route wrapper)
- **What:** list_builds() merges the in-memory cache (state.builds) with DB history via memory.recent_builds() (lines 170-176), so the dashboard/API can legitimately surface a build that exists ONLY in the persisted builds table (e.g. after a process restart the in-memory dict state.builds is empty — it is initialized fresh at deps.py:161 and never rehydrated from the DB on boot). But approve_build() looks up ONLY the in-memory cache: `rec = state.builds.get(build_id); if rec is None: raise KeyError(build_i
- **Fix:** In approve_build() (routes.py:249-252), fall back to the persisted DB record when the in-memory cache misses, seeding a BuildRecord into state.builds before proceeding:

```python
async def approve_build(state, build_id, approved=True, reason=""):
    rec = state.builds.get(build_id)
    if rec is None:
        # Reattach to a build that survived a restart (present in /api/builds via
        # DB augmentation but absent from the in-memory cache).
        if state.memory is not None and hasattr(s

## [MEDIUM] In-memory build cache is never rehydrated from the DB on boot — builds awaiting approval are silently lost after restart
- **Where:** skyn3t/web/deps.py:160-161 (AppState.__init__) and skyn3t/cli/main.py:691-697 (boot recovery)
- **What:** state.builds is created empty on every process start (deps.py:161) and is only ever populated by live BUILD_* events (deps.py:206-237). There is no code path that seeds it from memory.recent_builds() at startup. The CLI boot recovery (main.py:691-695) calls RecoveryManager().restore_and_announce(bus), which only replays the EventBus snapshot into memory and DISCARDS the returned RecoveryResult (the task_state/files_state describing the in-flight build) — it does not re-drive or re-register any b
- **Fix:** Two coordinated fixes:

1) Persist in-flight builds so they survive restart. In studio/runner.py, call `await self._save_build(manifest)` at build START (status='running') and again immediately before/after entering the approval gate (status='awaiting_approval'), not only at finalize. store.py:save_build already upserts by build_id, so repeated calls are safe.

2) Seed and re-drive the cache on boot. In AppState.__init__ (deps.py:161) — or in assemble_app_state after the AppState is built (main.

## [LOW] VisualChecker / visual_check is fully unwired — visual verification always soft-skips
- **Where:** skyn3t/studio/visual_check.py:71-76, :99-130
- **What:** visual_check.inspect() returns VisualVerdict(skipped=True, reason="no vision provider wired") whenever vision_fn is None, and VisualChecker.check() requires a vision_fn to do anything. A grep across skyn3t/ shows VisualChecker is never instantiated and vision_fn is never supplied by any caller (the only `.check(` hits are the unrelated budget.check()). So the entire screenshot+vision-judge visual verification capability is dead-wired: it can only ever return a skipped verdict in the running syst
- **Fix:** Wire the vision_fn so the capability can actually run, and integrate VisualChecker into a verification stage (the improve loop is the natural home, matching the module docstring).

1) Add a vision provider factory. In the LLM/model layer (wherever the Anthropic client is built — e.g. skyn3t/intelligence or the model router), add a callable that satisfies VisionFn = Callable[[str, str], str]: it base64-encodes the PNG at image_path and sends it as an image content block alongside the text prompt

## [LOW] ModelTournament is consumed by the learned router on every build but is only ever populated by the manual `skyn3t debate` CLI command — the build pipeline never feeds it
- **Where:** skyn3t/intelligence/debate.py:199-205 (only record_win caller) + skyn3t/cli/main.py:374-409 (only run_debate caller); consumer skyn3t/adapters/llm.py:121-122
- **What:** `ModelTournament.record_win` (model_tournament.py:121) is the only way the leaderboard gains data. The only caller is `Debate` (debate.py:203), and `run_debate` is only invoked from the standalone `skyn3t debate <question>` CLI command (cli/main.py:399-409). The build pipeline (studio/runner.py, core/orchestrator.py) never runs a debate and never calls record_win. Yet `_build_router` (llm.py:113-122) instantiates a `LearnedModelRouter` over this same tournament for production routing when model_
- **Fix:** Close the learning loop by having the build pipeline feed the tournament from real build outcomes, instead of (or in addition to) ad-hoc debates. Concretely, in StudioRunner._record_learning (skyn3t/studio/runner.py — the same place B1 already passes quality = manifest.score/100 to record_use), record a tournament outcome keyed by the actual task_type and tier used for the build's code/scaffold stages: call tournament.record_win(bucket_key(tier, task_type), winner=<model that produced the accept

## [LOW] delete_project active-build guard only consults in-memory state.builds — a persisted 'running' build can be trashed mid-run
- **Where:** skyn3t/web/routes.py:205-224 (delete_project), specifically the active-set computation at lines 212-215
- **What:** delete_project refuses deletion only when the slug matches a build in `state.builds` with status=='running'. The active set is built purely from the in-memory cache: `active = {getattr(r,'slug','') for r in state.builds.values() if getattr(r,'status','')=='running'}`. A build that is actually running but whose BuildRecord is not in this process's in-memory cache (e.g. started by a different worker/process, or a record evicted) — or more practically, the persisted-history path that list_builds su
- **Fix:** The right protective signal is the LIVE worktree directory, not a persisted 'running' status (which is never written mid-build). Harden delete_project (routes.py:205-224) to also refuse deletion when a live build worktree for the slug exists on disk:\n\n  wt_root = projects_root.parent / \".skyn3t_worktrees\"\n  worktree_live = (wt_root / target.name).is_dir()\n  if target.name in active or slug in active or worktree_live:\n      raise ValueError(\"project belongs to a running build\")\n\nThis c

## [LOW] brain_payload always reports lessons=0 (hardcoded stub) — dashboard Brain page shows a permanently false 0 lessons
- **Where:** skyn3t/web/routes.py:648-665 (brain_payload), lesson value fixed at line 651 & 663
- **What:** brain_payload initializes `lessons = 0` and never updates it; the returned dict hardcodes `"lessons": lessons` (= 0) regardless of how many graded lessons the learning loop has accumulated in memory. The memory store does hold lessons (store.py:119 add_lesson, :126 relevant_lessons), but brain_payload only queries recent_builds (for `documents`) and never counts lessons. The SPA Brain page (Brain.jsx:130) renders `data?.lessons` directly, so the 'lessons' stat is stuck at 0 forever.
- **Fix:** Add a total-count method to MemoryStore and call it from brain_payload.

1) In skyn3t/memory/store.py (lessons section, after relevant_lessons), add:

    async def count_lessons(self) -> int:
        from sqlalchemy import func, select
        async with self._session() as s:
            return int((await s.execute(select(func.count(LessonRow.id)))).scalar_one() or 0)

(func/select are already imported at module top — store.py:16 imports models and the file already uses select/update, so reuse

## [LOW] A running/in-progress build is never persisted, so a crash mid-build loses it entirely with no DB trace
- **Where:** skyn3t/studio/runner.py:708 (status set to 'running' in memory only) and runner.py:1147-1164 (_save_build call sites at 951, 969, 1171)
- **What:** runner._save_build() is the ONLY writer of the builds table (grep confirms save_build is called only from runner.py, and only at terminal states: _finalize line 1171, _BuildRejected line 951, generic-exception line 969). manifest.status is set to 'running' at line 708 but is NOT persisted at BUILD_STARTED. Therefore an in-flight build has NO row in the builds table until it reaches a terminal status. If the process is killed mid-build (OOM, SIGKILL, crash that bypasses the try/except), the build
- **Fix:** Persist the build at BUILD_STARTED and add a boot reconciliation sweep:

1. In runner.py, right after `manifest.status = "running"` (line 708) and before/after emitting BUILD_STARTED (~725), add `await self._save_build(manifest)`. Because `save_build` is an upsert keyed on build_id (store.py:97-100), the later terminal `_save_build` calls at 951/969/1171 will update the same row. This writes a 'running' row immediately so a hard crash leaves a DB trace.

2. Add a reconciliation pass invoked at M
