"""Central configuration for SkyN3t 2.0.

Pydantic Settings anchored to the repo root. Loads ``.env`` if present.
Default posture: free models on, autonomy gated, Docker sandbox preferred,
budgets enforced. Missing secrets never crash — features degrade instead.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (skyn3t/config/settings.py).
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Runtime configuration. Every field has a safe default."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_prefix="SKYN3T_",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Insert persisted Cortex tuning as a low-priority source.

        Precedence (highest first): explicit init kwargs > OS env > persisted
        tuning overrides > .env > secrets. So a tuned value carries into the next
        build, but an env var or explicit kwarg always wins. The overrides file
        is allow-list-filtered and read defensively (empty on any error), so this
        is a no-op when nothing has been tuned.
        """
        from pydantic_settings.sources import InitSettingsSource

        try:
            from skyn3t.cortex.tuning_store import load_overrides  # local: no import cycle

            data = load_overrides(REPO_ROOT / "data")
        except Exception:  # noqa: BLE001 - never let tuning break config construction
            data = {}
        overrides_source = InitSettingsSource(settings_cls, data)
        return (init_settings, env_settings, overrides_source, dotenv_settings, file_secret_settings)

    # ---- Identity / paths ------------------------------------------------
    app_name: str = "SkyN3t"
    version: str = "2.0.0"
    data_dir: Path = REPO_ROOT / "data"
    projects_dir: Path = REPO_ROOT.parent / "Projects"
    logs_dir: Path = REPO_ROOT / "logs"

    # ---- Database / vector store ----------------------------------------
    db_url: str = ""  # resolved in __init__ if empty
    vector_db_path: Path = REPO_ROOT / "data" / "vector_db"

    # ---- Web -------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 6660
    auth_token: str = ""  # empty => loopback-only access enforced by app

    # ---- LLM backends ----------------------------------------------------
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    kimi_api_key: str = ""

    # ---- Image generation (Replicate) -----------------------------------
    # Token (env SKYN3T_REPLICATE_API_TOKEN) for real image generation. When set,
    # an image-implying brief (e.g. a kids coloring app) can generate real line-art
    # assets instead of "crappy drawings". Empty -> image-gen is skipped entirely;
    # it never blocks or fails a build. The asset-generation STEP is additionally
    # gated behind `asset_gen` (default off) so the capability stays opt-in + cost-
    # aware. ``replicate_model`` overrides the default (black-forest-labs/flux-schnell
    # — fast, cheap, line-art-capable); use an "owner/name" official model id.
    replicate_api_token: str = ""
    replicate_model: str = ""

    # ---- Deploy (Ship pillar) -------------------------------------------
    # Per-provider tokens for a REAL, token-gated deploy. Empty => the keyless
    # plan / dry-run only (never blocks). Configure via the GUI Settings page —
    # never hardcoded/env-required (the GUI-first config rule). Each provider CLI
    # reads its own env var (FLY_API_TOKEN / VERCEL_TOKEN / CLOUDFLARE_API_TOKEN),
    # which is injected into a SCRUBBED subprocess so ONLY that one token crosses.
    # ``allow_remote_deploy`` is the master gate: off (default) => plan only, a
    # real provider deploy is never fired.
    fly_api_token: str = ""
    vercel_token: str = ""
    cloudflare_api_token: str = ""
    allow_remote_deploy: bool = False

    # ---- CLI LLM backends (no API key; use locally-installed CLIs) -------
    # auto picks: openrouter (if key) -> a detected CLI -> stub.
    llm_backend: str = "auto"  # auto|stub|openrouter|claude_cli|kimi_cli|copilot_cli|openai_cli
    cli_llm_provider: str = "claude"  # which CLI when auto selects one
    # Route ONLY the codegen (code agent) stage to a coding-agent CLI's agentic
    # whole-app build, while every OTHER stage keeps the global backend (e.g. cheap
    # OpenRouter models). Empty = no override (codegen follows the global backend).
    # Set to "claude" to get high-quality codegen on the authed claude CLI without
    # paying for claude on brainstorm/architecture/review/docs.
    codegen_cli_provider: str = ""
    # Pin the model the above CLI runs codegen on (e.g. "sonnet"). Accepts the
    # CLI's own aliases (sonnet/opus/fable) or a full model id. Empty = no pin,
    # the CLI's own default model applies. Only used when codegen_cli_provider
    # (or the global CLI backend) is set; ignored otherwise.
    codegen_cli_model: str = ""
    # Pin the OpenRouter model skyn3t uses (any id from the live /models list, e.g.
    # "openai/gpt-4o" or "deepseek/deepseek-chat"). Empty = AUTO: the learned router
    # picks per tier/task (today's behaviour). Set = that model for every OpenRouter
    # completion (vision still resolves its own model for image calls). Chosen in
    # Settings via the model dropdown; overridable per-call by model_override.
    preferred_model: str = ""
    # Pin OpenRouter whole-project codegen to a specific model. Empty means use
    # the router's backend tier pick. This is separate from ``codegen_cli_model``
    # because the CLI override and OpenRouter path are mutually exclusive.
    openrouter_codegen_model: str = ""
    # First-class per-tier model pins. These outrank persisted
    # data/model_tier_overrides.json so dashboard/env choices are visible and
    # predictable.
    model_cheap: str = ""
    model_ui: str = ""
    model_backend: str = ""
    model_strong: str = ""
    model_docs: str = ""
    # Give OpenRouter (cheap) models a whole-project AGENTIC codegen loop — the
    # model writes files itself via tool-calls with full context (like bolt/v0/
    # Aider), instead of the weak per-file generation. This is what lets cheap
    # models build coherent full apps. Max tool-call turns bounds it.
    openrouter_agentic: bool = True
    openrouter_agentic_max_turns: int = 60
    # Verify-on-stop (research item 19, degrade-open): when the agentic model
    # calls `finish`, run a cheap static scan (unresolved local imports + Python
    # syntax) and DENY the finish with the real defect list — at most twice,
    # then accept and let the pipeline fix-loop take over. Kills the
    # dangling-import class inside codegen, where context is still warm.
    agentic_verify_on_stop: bool = True
    cli_llm_timeout: int = 300  # generating a substantial file via claude -p needs room
    # Run the headless build CLIs (claude/kimi -p) WITHOUT the host's ambient MCP
    # servers. Otherwise every codegen call boots the user's whole ~/.claude MCP
    # fleet (Aruba, context7, playwright, ...) — pure per-build startup tax and a
    # sandboxing concern. True = isolated builds; flip to False only to give the
    # build agent your MCP tools on purpose.
    cli_disable_mcp: bool = True
    # Extra agentic codegen attempts when the first `claude -p` under-delivers
    # (produced only a placeholder/stub). Each retry re-runs with corrective
    # feedback. Fires ONLY on under-delivery, so a good first pass adds no time.
    agentic_retries: int = 1
    # Wall-clock budget (seconds) for ONE agentic `claude -p` codegen session.
    # A full multi-file app routinely needs >15 min; the old cli_llm_timeout*3
    # killed it mid-build and shipped a stub. 0 falls back to cli_llm_timeout*3.
    agentic_build_timeout: int = 1800
    # Stall guard (seconds) for a streaming agentic session: if the agent emits
    # NO stream events for this long it has hung, so we kill it early instead of
    # burning the full build budget. A working `claude -p` emits message/tool
    # events far more often than this, so it never trips on real progress. 0
    # disables the guard (only the hard ``agentic_build_timeout`` ceiling applies).
    agentic_idle_timeout: int = 600
    # Route dashboard Improve goals through the whole-project agentic tool-loop
    # (same machinery as builds) so a feature goal can CREATE new pages and touch
    # multiple files, instead of one entrypoint rewrite. The classic per-file
    # improver remains the automatic fallback when agentic is unavailable, fails,
    # or lands no changes — turning this on can't do worse than before it existed.
    improve_agentic: bool = True
    # Wall-clock budget (seconds) for one agentic improve session. Improves are
    # scoped changes to an existing app — far smaller than a full build (1800s).
    improve_agentic_timeout: int = 900
    # Hermes-style orchestrator-worker codegen: decompose a non-trivial build into
    # parallel scoped sub-agents (frontend / backend / tests / config), each in its
    # own worktree, then merge + let the (error-aware) proof/fix-loop wire them.
    # OFF by default — it only helps apps with enough independent files; tiny apps
    # keep the monolithic path. ``_min_files`` is the architect-file floor below
    # which slicing is skipped (cold-start + merge overhead would dominate).
    parallel_code_slices: bool = False
    parallel_code_slices_min_files: int = 8
    # Optional per-slice-tier model pins for the single-model agentic CLI
    # ({"strong": "...", "ui": "...", "cheap": "..."}) — the "mixed by slice"
    # routing. Empty -> every slice uses the default model (still fully parallel).
    # On the OpenRouter backend per-file tier routing already mixes models, so
    # this only affects the agentic path.
    slice_tier_models: dict[str, str] = {}

    # ---- LLM call resilience (deep-dive item 2: fallback + retry + ctx-edit) --
    # Model failover (langchain ModelFallbackMiddleware): when a routed model
    # fails with a model-level error (retired :free 404, invalid id) or persistent
    # transient errors, fall over through an ordered candidate list. This is the
    # guard against the `deepseek-*:free` 404 that once silently degraded every
    # build. Candidates = this comma-list (tried first) + the router's per-tier
    # picks. Empty list -> router-derived candidates only.
    llm_fallback_enabled: bool = True
    llm_fallback_models: str = ""
    # Bounded transient retry BEFORE failover (langchain retry w/ backoff+jitter):
    # 429 / 5xx / timeouts / connection errors are retried on the SAME model with
    # exponential backoff + jitter; genuine bad requests (400 valid model) and auth
    # (401/403) fail fast. ``base``/``max`` are the backoff bounds in seconds.
    llm_retry_enabled: bool = True
    llm_max_retries: int = Field(default=3, ge=0, le=8)
    llm_retry_base_delay: float = Field(default=0.5, ge=0.0)
    llm_retry_max_delay: float = Field(default=8.0, ge=0.0)
    # Agentic tool-loop context editing (langchain ClearToolUsesEdit): when the
    # SENT history exceeds this byte budget, OLD tool-result file dumps (read_file/
    # list_files output) are replaced with a short stub on a COPY of the history —
    # keeping the system prompt, the user goal, and the most recent K tool results
    # intact. Kills the read-dump x long-session context blowout on cheap models.
    agentic_context_editing: bool = True
    agentic_context_budget_bytes: int = Field(default=200_000, ge=0)
    agentic_context_keep_last: int = Field(default=6, ge=0)

    # GitHub token (env SKYN3T_GITHUB_TOKEN) for RepoScout search + repo ingest.
    # Authenticated search lifts the rate limit so scouting returns real, varied
    # repos instead of degrading to the built-in seed list.
    github_token: str = ""

    # ---- Routing policy --------------------------------------------------
    free_only: bool = True
    no_claude: bool = False
    model_evolution: bool = False  # opt-in: with auto_route, route via the learned ModelTournament router
    openrouter_max_concurrency: int = Field(default=4, ge=1)

    # ---- Cost caps (hard backstops) -------------------------------------
    # 0 disables the per-build cap; daily USD and token caps still apply.
    per_build_usd_cap: float = 0.0
    daily_usd_cap: float = 5.00
    daily_token_cap: int = 5_000_000
    autonomous_daily_build_cap: int = 10

    # ---- Feature flags ---------------------------------------------------
    debate_enabled: bool = False  # opt-in: when on, `skyn3t debate` runs a full multi-model debate
    a2a_conversation: bool = False  # opt-in alias for debate-style agent-to-agent conversation
    reflective_retry: bool = True
    auto_route: bool = False  # opt-in: gates the learned router; enable together with model_evolution
    asset_gen: bool = False
    # Game art tier (#6): role sprites for game stacks. `game_art_enabled` on by
    # default (the floor degrades to clean colored primitives at $0). `game_art_source`
    # picks where sprites come from: "offline" (deterministic local PNGs, free),
    # "kenney" (installed CC0 packs in data/asset_packs, free), "replicate"
    # (themed sprites generated at build time, ~cents), or "auto"
    # (Kenney when installed, else replicate when configured, else offline).
    game_art_enabled: bool = True
    game_art_source: str = "auto"  # offline | kenney | replicate | auto
    # When on, a cheap LLM art-director (one call/build, gated here) tailors a game's
    # roles + palette to the brief for the long tail of games the deterministic
    # planner doesn't recognize (e.g. fishing -> boat/fish/hook). Off -> the
    # deterministic genre/open-ended floor (no call, $0). Never blocks a build: any
    # failure falls back to the floor. Default ON for game builds — costs nothing on
    # the stub backend (returns the $0 floor), tailors art only when a real LLM exists.
    art_director_enabled: bool = True
    # When on, a cheap LLM game-designer (one call/build, gated here) tailors the
    # game's DEPTH spec (named power-ups, levels, economy) to the brief; off -> the
    # deterministic genre-aware GDD floor (no call, $0). The depth contract (>=2
    # power-ups, win+lose) is guaranteed either way. Never blocks a build. Default ON
    # for game builds — $0 on the stub backend, deepens design only with a real LLM.
    game_designer_enabled: bool = True
    # When on (and a vision model is reachable), a game build's delivered, RUNNING
    # game is screenshotted mid-play and judged by a vision model for the things
    # headless gates fundamentally can't see — an EMPTY play field / TINY entities /
    # "nothing to play" (a human catches these by looking). ADVISORY: recorded to the
    # manifest and fed to the fix-loop as guidance; it NEVER blocks the verdict, and
    # soft-skips with no vision model. Default ON for game builds: it checks (no key is
    # needed BEFORE it serves — without a vision model it returns immediately, $0 + no
    # added time), and adds the serve + screenshot + a vision call only when a vision
    # model is configured.
    game_visual_check_enabled: bool = True
    # When on (and a vision model + the code-improver are available), the game visual
    # check ACTS — it feeds an EMPTY/TINY gap to the improver and keeps the repair only
    # if it preserves headless-gate correctness, improves the visual verdict, and still
    # builds (else rolls back). Off => the check stays advisory/record-only. Never blocks
    # the verdict. Default ON for game builds — a no-vision-model run never judges, so
    # there is nothing to repair (no-op, $0).
    game_visual_repair_enabled: bool = True
    # QA-playtest gate (roadmap #9): serves the built game and DRIVES every control with
    # Playwright — movement, fire, the off-contract barrel-roll (Z/Shift), pause — long
    # enough to spawn a wave, failing on any uncaught console/page error (the freeze/
    # ReferenceError class the sim gate's {left,right,up,down,action,pause} contract never
    # triggers); also verifies generated sprites are actually preloaded/rendered. ADVISORY:
    # recorded to the manifest and fed to the fix-loop; it NEVER blocks the verdict by
    # itself. Default ON for game builds — it checks Playwright availability BEFORE it
    # serves, so without Playwright it soft-skips immediately ($0 + no added time).
    qa_playtest_enabled: bool = True
    # Master switch that turns the (otherwise advisory) game_visual + qa_playtest
    # checks into HARD verdict gates for game stacks. When on, a REAL, non-skipped
    # failure (game_visual.ok is False / qa_playtest.ok is False) forces no_go (and
    # the score auto-clamps to <=49). Safe by construction: a SKIPPED check (no vision
    # model / no Playwright) never blocks, so offline builds are unaffected. Default ON
    # — this is the fix for "a broken game still scored go". Set 0 to restore advisory.
    game_quality_gates_verdict: bool = True
    best_of_n: int = Field(default=2, ge=1, le=8)  # 2.0: default best-of-2 trajectory sampling
    # opt-in: when best_of_n>1, pin each trajectory to a DIFFERENT model from
    # tournament_model_pool so the run is a real cross-model contest (genuine
    # comparative Elo + best output), not the same model N times.
    best_of_n_across_models: bool = False
    # Comma-separated model ids the cross-model sampler draws from (filtered by
    # free_only/no_claude). Empty → sampler degrades to same-model (no-op).
    tournament_model_pool: str = ""
    critic_enabled: bool = True  # 2.0: adversarial pre-delivery critic
    # The critic runs (records issues) but is ADVISORY by default — it no longer
    # hard-gates the verdict. It was forcing no_go on verified-running apps via
    # false "blocking" issues manufactured by reading files truncated mid-source.
    # Set True to restore the hard gate.
    critic_gates_verdict: bool = False
    # Spec 2: N-ensemble vote for the intent judge (median of N samples, robust
    # to an outlier). 1 = single call (default); >1 costs N judge calls/build.
    intent_judge_samples: int = Field(default=1, ge=1, le=7)
    # Spec 4: autonomous fan-out. Comma-separated stacks an UNPINNED build is
    # explored across (build each, deliver the proof-winner). Empty = OFF
    # (default; a normal single build). e.g. "react,static,fastapi". Costs N
    # builds, so it's an explicit opt-in.
    autonomous_fanout_stacks: str = ""
    # Descriptive build metadata overrides. "auto" keeps inference from the brief
    # + stack; set via env/UI when a user wants a specific app-type or engine label
    # without hardcoding it in prompts or scaffolds.
    app_type_override: str = "auto"
    engine_override: str = "auto"
    # Spec 3: vision model (OpenRouter id) for the visual loop's judgement step.
    # Empty -> a built-in default; the loop activates only when openrouter_api_key
    # is also set, else it soft-skips the screenshot judgement.
    vision_model: str = ""
    # End-of-build liveness loop (web stacks): enumerate the delivered app's
    # routes/pages, hit each, repair failures, and dampen the score by how many
    # respond. liveness_gates_verdict opts into a hard no_go when a route stays
    # dead after repair (default: dampen the score but don't flip the verdict).
    liveness_check_enabled: bool = True
    liveness_gates_verdict: bool = False
    liveness_max_rounds: int = 2
    # Advisory SEO check (web/HTML stacks): a DETERMINISTIC static scan of the delivered
    # source + built output for the cheap, unambiguous SEO signals — a non-empty <title>
    # (literal or Next.js metadata), a meta description, one <h1>, an <html lang>, Open
    # Graph basics, <img> alt coverage, and robots.txt/sitemap.xml. Unlike the game
    # checks it needs no Playwright/vision (SEO signals are static + free), so it defaults
    # ON. ADVISORY only: findings are recorded to manifest.extra["seo"] and fed to the
    # improver; it NEVER flips the verdict (a static SEO nit must not no_go a working app).
    seo_check_enabled: bool = True
    # Deterministic MCP-server gate (mcp stack, wave-2 §3.3): spawn the delivered
    # server.py and drive the real Model Context Protocol over stdio (initialize →
    # tools/list → tools/call each tool → one malformed call). ZERO LLM. Like the
    # SEO check records findings to manifest.extra["mcp_check"] and feeds ONE
    # repair (snapshot → improve → re-proof → keep or roll back). When
    # ai_native_gates_verdict is on, a real non-skipped MCP/RAG/workflow contract
    # failure blocks the final verdict; soft-skips never block.
    mcp_check_enabled: bool = True
    # Deterministic RAG-app gate (rag stack, wave-2 §3.1): boot the delivered
    # FastAPI app and drive the real HTTP contract (/health → /v1/stats → /ingest
    # a marker doc → /query must retrieve it → /chat answers → one malformed
    # ingest must yield a structured 4xx). ZERO LLM (the scaffold's /chat degrades
    # to extractive answers with no key). Records to manifest.extra["rag_check"]
    # and feeds ONE repair; ai_native_gates_verdict decides whether real
    # non-skipped findings block the final verdict.
    rag_check_enabled: bool = True
    # Deterministic agent-workflow gate (workflow stack, wave-2 §3.2): boot the
    # delivered runner (WEBHOOK_URL + LLM seams scrubbed) and drive the spec's
    # /trigger contract (dry-run envelope → live-unconfigured must yield
    # skipped_no_delivery not a crash → ledger recorded both → unknown workflow
    # rejected 4xx). Records to manifest.extra["workflow_check"], feeds ONE
    # snapshot/re-proof/rollback repair, and can hard-gate via
    # ai_native_gates_verdict. Soft-skips when deps aren't importable.
    workflow_check_enabled: bool = True
    ai_native_gates_verdict: bool = True
    security_check_enabled: bool = True
    web_polish_gate_enabled: bool = True
    # Deterministic CLI gate (python_cli family, wave-2 §3.6 tier): drive the
    # delivered main.py's command surface with bounded subprocess calls —
    # --help must work, every advertised subcommand's --help must work, and
    # (on subcommand CLIs) invalid input must be rejected with a nonzero exit
    # + stderr. ADVISORY like its siblings: recorded to
    # manifest.extra["cli_check"], one repair, never flips the verdict.
    cli_check_enabled: bool = True
    # Deterministic deploy_check gate (Ship pillar): after a REAL deploy, re-run
    # the liveness/contract probes against the LIVE url (not localhost). Opt-in
    # (default OFF) — it only applies once something is actually deployed. ADVISORY
    # like its siblings: recorded to manifest.extra["deploy_check"], never flips
    # the verdict (a proven build already shipped).
    deploy_check_enabled: bool = False
    # Execute the GENERATED project's own test suite during the proof (pytest /
    # npm test), bounded + guarded. A real failure fails the proof and routes
    # into the fix loop — "verify behavior, not vibes". Kill-switch for CI/offline.
    run_generated_tests: bool = True
    # For generated Python apps, install a small declared dependency set before
    # boot/tests when proof needs to run code. The install is bounded and uses the
    # same sandbox/filtered env path as other proof commands.
    proof_install_python_deps: bool = True
    proof_python_deps_timeout: int = 120
    # Reliability flywheel: when a REAL build finishes no_go/failed, append it as a
    # permanent bench regression case (bench.capture_regression_case) so a future
    # ratchet run must keep it green. On by default: captured failures feed the
    # autonomy gate via bench.all_cases().
    bench_capture_failures: bool = True
    generated_test_timeout: int = 90
    # Mock-LLM proof seam (research item 42): when the generated project CALLS an
    # LLM (openai/anthropic client dep or an OPENAI_BASE_URL/OPENROUTER_API_KEY
    # reference), boot a local deterministic OpenAI/Anthropic-compatible mock
    # server for the project's OWN test step and inject OPENAI_BASE_URL/
    # ANTHROPIC_BASE_URL + dummy keys, so an LLM app is provable headlessly with
    # ZERO API spend. Advisory + degrade-open: never changes the build step, and
    # if the mock can't start (or a network-isolated sandbox is in use) the tests
    # run exactly as before. Off restores the pre-seam behavior.
    mock_llm_proof_enabled: bool = True
    # Compile node/react builds for real in the proof (npm install + npm run
    # build / typecheck), bounded + guarded. Catches type/build errors a static
    # check misses; soft-skips offline (no npm / registry). The build error is
    # fed to the fix loop as a gap so a real backend can repair it.
    run_generated_build: bool = True
    generated_build_timeout: int = 300
    # A go earned under degraded proof evidence (for example build skipped due to
    # missing/offline tooling) remains go, but its success-looking score is capped.
    degraded_proof_score_cap: float = Field(default=74.0, ge=0, le=100)
    # Convergence loop: keep re-running build -> feed the exact error to the improver
    # -> retry until the proof passes or these bounds hit. The cheap model emits a
    # different defect each build; a multi-error cascade (e.g. styled-jsx + a bad
    # import) needs more than 2 passes to clear. Bounded by attempts AND wall-clock.
    max_fix_attempts: int = 6
    fix_loop_budget_s: int = 720
    # Headless invariant gate (game stacks): run the pure sim core (src/sim.js) in
    # Node and assert hard runtime invariants (NaN/pool-leak/determinism/pause/
    # game-over). Violations feed the improver like compile errors; unresolved
    # ones block the verdict. Game-stacks only; non-games + games without a sim
    # core are unaffected.
    headless_gate_enabled: bool = True
    headless_gate_attempts: int = 3
    # Opt-in strengthening: the headless harness records winReachable/loseReachable
    # but its pass/fail is driven by invariant violations alone, so a game you can
    # neither win nor lose still passes. When on, an APPLICABLE gate with BOTH
    # win+lose unreachable blocks the verdict (a game with no reachable ending isn't a
    # game). Default OFF: the reachability probe can't always reach an ending in a
    # legit game, so this stays opt-in to avoid a false no_go.
    headless_gate_requires_reachable: bool = False
    visual_self_heal: bool = False  # opt-in: drive rendered UI + repair with vision (needs browser)
    visual_self_heal_max_rounds: int = Field(default=2, ge=1, le=5)
    reward_hardening: bool = True  # 2.0: anti-reward-hacking on graders

    # ---- Autonomy --------------------------------------------------------
    autonomous_builds: bool = False
    autonomous_learning: bool = True
    approval_gates: bool = True
    cortex_auto_approve_safe: bool = True
    # CuriosityLoop emits a generic, target-less "scout for new build patterns"
    # INGEST proposal every hour — always gated, regenerated after each decision,
    # and redundant with RepoScout (which scouts real, named repos). Off by
    # default so it stops nagging for approvals; enable for periodic curiosity.
    curiosity_loop_enabled: bool = False
    # Reliability ratchet (the flywheel's decision brain): keep a proposed
    # tuning/prompt change only if a bench run measurably raises the go-rate (no
    # aggregate OR per-app-type regression), else revert. Opt-in (default off) —
    # running it does real builds (cost/time). When enabled, Cortex routes
    # auto-applyable tuning through it; `cortex ratchet` can also run it manually.
    reliability_ratchet_enabled: bool = False

    # ---- Sandbox ---------------------------------------------------------
    execution_backend: str = "auto"  # auto | docker | inline
    sandbox_hardening: bool = True
    sandbox_drop_caps: bool = True

    # ---- Learning corpus -------------------------------------------------
    skills_hub_paths: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.db_url:
            self.db_url = f"sqlite+aiosqlite:///{self.data_dir / 'skyn3t.db'}"
        for p in (self.data_dir, self.logs_dir, self.vector_db_path):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def claude_available(self) -> bool:
        return bool(self.anthropic_api_key) and not self.no_claude

    @property
    def replicate_available(self) -> bool:
        """True when a Replicate token is configured — the build then KNOWS it can
        generate real images. The asset-gen step additionally requires asset_gen."""
        return bool(self.replicate_api_token)

    @property
    def deploy_tokens(self) -> dict[str, str]:
        """Configured per-provider deploy tokens keyed by provider name. Empty
        dict => no real deploy is possible (the keyless plan is all you get). Used
        by DeployAgent to pick + inject the one token for the chosen provider."""
        return {
            name: tok
            for name, tok in (
                ("fly", self.fly_api_token),
                ("vercel", self.vercel_token),
                ("cloudflare", self.cloudflare_api_token),
            )
            if tok
        }

    @property
    def has_any_llm(self) -> bool:
        return bool(
            self.openrouter_api_key
            or self.anthropic_api_key
            or self.openai_api_key
            or self.kimi_api_key
        )


@lru_cache
def get_settings() -> Settings:
    """Cached singleton accessor."""
    return Settings()
