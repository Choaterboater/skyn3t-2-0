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

    # ---- CLI LLM backends (no API key; use locally-installed CLIs) -------
    # auto picks: openrouter (if key) -> a detected CLI -> stub.
    llm_backend: str = "auto"  # auto|stub|openrouter|claude_cli|kimi_cli|copilot_cli|openai_cli
    cli_llm_provider: str = "claude"  # which CLI when auto selects one
    cli_llm_timeout: int = 300  # generating a substantial file via claude -p needs room

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
