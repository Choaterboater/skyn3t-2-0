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
    # Give OpenRouter (cheap) models a whole-project AGENTIC codegen loop — the
    # model writes files itself via tool-calls with full context (like bolt/v0/
    # Aider), instead of the weak per-file generation. This is what lets cheap
    # models build coherent full apps. Max tool-call turns bounds it.
    openrouter_agentic: bool = True
    openrouter_agentic_max_turns: int = 60
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
    per_build_usd_cap: float = 0.50
    daily_usd_cap: float = 5.00
    daily_token_cap: int = 5_000_000
    autonomous_daily_build_cap: int = 10

    # ---- Feature flags ---------------------------------------------------
    debate_enabled: bool = False  # opt-in: when on, `skyn3t debate` runs a full multi-model debate
    a2a_conversation: bool = False
    reflective_retry: bool = True
    auto_route: bool = False  # opt-in: gates the learned router; enable together with model_evolution
    asset_gen: bool = False
    best_of_n: int = Field(default=1, ge=1, le=8)  # 2.0: trajectory sampling
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
    # Execute the GENERATED project's own test suite during the proof (pytest /
    # npm test), bounded + guarded. A real failure fails the proof and routes
    # into the fix loop — "verify behavior, not vibes". Kill-switch for CI/offline.
    run_generated_tests: bool = True
    generated_test_timeout: int = 90
    # Compile node/react builds for real in the proof (npm install + npm run
    # build / typecheck), bounded + guarded. Catches type/build errors a static
    # check misses; soft-skips offline (no npm / registry). The build error is
    # fed to the fix loop as a gap so a real backend can repair it.
    run_generated_build: bool = True
    generated_build_timeout: int = 300
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
    visual_self_heal: bool = False  # 2.0: drive rendered UI (needs browser)
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

    # ---- Sandbox ---------------------------------------------------------
    execution_backend: str = "auto"  # auto | docker | inline
    sandbox_hardening: bool = True
    sandbox_drop_caps: bool = True

    # ---- Learning corpus -------------------------------------------------
    learnings_dir: Path | None = None
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
