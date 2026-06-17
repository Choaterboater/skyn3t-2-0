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

    # ---- Routing policy --------------------------------------------------
    free_only: bool = True
    no_claude: bool = False
    model_evolution: bool = True
    openrouter_max_concurrency: int = Field(default=4, ge=1)

    # ---- Cost caps (hard backstops) -------------------------------------
    per_build_usd_cap: float = 0.50
    daily_usd_cap: float = 5.00
    daily_token_cap: int = 5_000_000
    autonomous_daily_build_cap: int = 10

    # ---- Feature flags ---------------------------------------------------
    debate_enabled: bool = False
    a2a_conversation: bool = False
    reflective_retry: bool = True
    auto_route: bool = True
    asset_gen: bool = False
    best_of_n: int = Field(default=1, ge=1, le=8)  # 2.0: trajectory sampling
    critic_enabled: bool = True  # 2.0: adversarial pre-delivery critic
    visual_self_heal: bool = False  # 2.0: drive rendered UI (needs browser)
    reward_hardening: bool = True  # 2.0: anti-reward-hacking on graders

    # ---- Autonomy --------------------------------------------------------
    autonomous_builds: bool = False
    autonomous_learning: bool = True
    approval_gates: bool = True
    cortex_auto_approve_safe: bool = True

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
