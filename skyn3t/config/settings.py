"""Central configuration for SkyN3t 2.0.

Pydantic Settings anchored to the repo root. Loads ``.env`` if present.
Default posture: free models on, autonomy gated, Docker sandbox preferred,
usage observed without hard spend/token/build ceilings. Missing secrets never
crash — features degrade instead.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

import structlog
from pydantic import Field, TypeAdapter, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = structlog.get_logger(__name__)


def _resolve_runtime_layout(
    package_file: str | Path,
    *,
    home: str | Path | None = None,
) -> tuple[Path, Path]:
    """Return the state root and projects directory for this installation."""
    package_path = Path(package_file).resolve()
    checkout_root = package_path.parents[2]
    if (checkout_root / "pyproject.toml").is_file() and (
        checkout_root / "skyn3t" / "__init__.py"
    ).is_file():
        return checkout_root, checkout_root.parent / "Projects"

    state_root = Path(home).expanduser() if home is not None else Path.home()
    state_root = state_root / ".skyn3t"
    return state_root, state_root / "Projects"


# Checkouts keep their historical repo-relative layout. Installed wheels must
# never try to write mutable state into site-packages.
REPO_ROOT, DEFAULT_PROJECTS_DIR = _resolve_runtime_layout(__file__)


def _validated_tuning_overrides(settings_cls, data: dict) -> dict:
    """Drop persisted tuning values the target field would reject.

    ``load_overrides`` allow-lists KEYS only; a hand-edited
    ``settings_overrides.json`` with a bad VALUE (``{"best_of_n": "x"}``)
    previously raised ``ValidationError`` out of every ``get_settings()``
    caller — the exact crash the surrounding try/except promises to prevent.
    Trial-validate each value against its field (constraints included) so a
    bad value costs that one override, never config construction.
    """
    out: dict[str, Any] = {}
    for key, value in data.items():
        field_info = settings_cls.model_fields.get(key)
        if field_info is None:
            continue
        annotation = field_info.annotation if field_info.annotation is not None else Any
        if field_info.metadata:
            annotation = Annotated[tuple([annotation, *field_info.metadata])]
        try:
            TypeAdapter(annotation).validate_python(value)
        except Exception as exc:  # noqa: BLE001 - drop the override, keep booting
            log.warning("settings.tuning_override_dropped", key=key,
                        error=str(exc)[:120])
            continue
        out[key] = value
    return out


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

            # The allow-list filters keys; _validated_tuning_overrides filters
            # VALUES — both are needed for the "never break construction"
            # promise, because pydantic validates these after this hook.
            #
            # Root resolution mirrors the writers (cli/main.py and
            # cortex/bootstrap.py persist to ``settings.data_dir``) and
            # pydantic's own precedence for that field: OS env first, then the
            # .env file, else the repo default. Reading only REPO_ROOT/data
            # meant that with SKYN3T_DATA_DIR set, persisted tuning was
            # written to one root and silently never applied from the other
            # (audit M2); reading only OS env left the flagship .env surface
            # (.env.example documents SKYN3T_DATA_DIR) with the same split.
            # An init-kwarg data_dir remains unknowable this early.
            data_root = os.environ.get("SKYN3T_DATA_DIR", "").strip()
            if not data_root:
                try:
                    # dotenv source keys are FIELD names (prefix stripped):
                    # a .env SKYN3T_DATA_DIR arrives as "data_dir".
                    data_root = str(dotenv_settings().get("data_dir") or "").strip()
                except Exception:  # noqa: BLE001 - a bad .env must not cost tuning for default roots
                    data_root = ""
            data = _validated_tuning_overrides(
                settings_cls, load_overrides(data_root or REPO_ROOT / "data")
            )
        except Exception:  # noqa: BLE001 - never let tuning break config construction
            data = {}
        overrides_source = InitSettingsSource(settings_cls, data)
        return (init_settings, env_settings, overrides_source, dotenv_settings, file_secret_settings)

    @field_validator("llm_backend", "execution_backend", "game_art_source", mode="before")
    @classmethod
    def _normalize_choice_fields(cls, value):
        """Keep the historical tolerance for case/whitespace on choice fields.

        These were free strings that every consumer ``.strip().lower()``-ed;
        now that they are Literal-typed, ``Auto`` must still mean ``auto`` —
        only genuinely unknown values should fail construction.
        """
        return value.strip().lower() if isinstance(value, str) else value

    # ---- Identity / paths ------------------------------------------------
    app_name: str = "SkyN3t"
    version: str = "2.0.0"
    data_dir: Path = REPO_ROOT / "data"
    projects_dir: Path = DEFAULT_PROJECTS_DIR
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
    # A dashboard disconnect must also suppress the supported native
    # OPENROUTER_API_KEY fallback without destroying an externally managed
    # credential. Saving a key through Settings turns this back on.
    openrouter_enabled: bool = True
    # Stored for redaction seeding (SecretsStore) and possible future native
    # backends — NO current backend consumes these three; only the OpenRouter
    # key above (and the local CLIs' own logins) can actually generate.
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
    # reads its own canonical env var, which is injected into a SCRUBBED
    # subprocess so ONLY that one provider token crosses.
    # ``allow_remote_deploy`` is the master gate: off (default) => plan only, a
    # real provider deploy is never fired.
    fly_api_token: str = ""
    vercel_token: str = ""
    cloudflare_api_token: str = ""
    netlify_auth_token: str = ""
    railway_token: str = ""
    render_api_key: str = ""
    allow_remote_deploy: bool = False

    # ---- CLI LLM backends (no API key; use locally-installed CLIs) -------
    # ``auto`` resolves to the first signed-in CLI in ``auto_cli_priority``. It
    # never uses OpenRouter merely because a key is configured: a key is
    # configuration, not consent to spend. Select ``openrouter`` explicitly, or
    # set ``auto_allow_openrouter``, when a hosted provider is intended.
    # Mirrors SUPPORTED_LLM_BACKENDS in skyn3t/adapters/llm.py (settings cannot
    # import the adapter). Literal-typed so a typo like ``claude`` (missing
    # ``_cli``) fails construction loudly instead of silently degrading the
    # whole system to the offline stub.
    llm_backend: Literal[
        "auto", "stub", "openrouter",
        "codex_cli", "claude_cli", "kimi_cli", "copilot_cli",
    ] = "auto"
    # Order ``auto`` tries local CLIs in. Codex leads only because it was the
    # historical default, so existing hosts see no change — there is nothing
    # special about it, and reordering this is the supported way to prefer a
    # different provider for unattended builds. Unknown entries are ignored.
    # ``copilot`` is deliberately absent from the default chain (add it here to
    # opt in). ``claude`` is absent too: nothing Claude-powered runs unless the
    # operator selects it (``llm_backend=claude_cli``, a slot, or an entry
    # here). Both remain fully supported and explicitly addressable.
    auto_cli_priority: str = "codex,kimi"
    # Explicit consent for ``auto`` to fall back to hosted OpenRouter when NO
    # local CLI is available. Off by default: without it, ``auto`` degrades to
    # the offline stub rather than silently spending.
    auto_allow_openrouter: bool = False
    # Used by manually selected CLI-adjacent features (for example vision
    # checks). Automatic build execution walks ``auto_cli_priority`` (any
    # available CLI in that chain satisfies the routing lock), never this field.
    cli_llm_provider: str = "codex"
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
    # Task-specialized model routing (evidence: OpenHands SDK paper, MLSys 2026,
    # Table 5 — models diverge sharply by TASK TYPE: one model leads GREENFIELD
    # codegen, another leads ISSUE-RESOLUTION/repair). SkyN3t's loop is exactly
    # those two task types: ``codegen_model_slot`` pins the greenfield path
    # (code_agent's agentic whole-app build), ``repair_model_slot`` pins the
    # repair path (code_improver's fix-loop rewrites + agentic improve). Both
    # use the SAME ``provider:model`` grammar as the MoA council
    # (skyn3t/adapters/model_slot.py): "openrouter:deepseek/deepseek-v4-flash",
    # "claude_cli:sonnet", a bare provider ("kimi_cli"), or a bare model id
    # pinned on the active backend. Empty (default) = today's tier-routed
    # behavior, byte-identical. When set, the slot is authoritative for its
    # path ONLY: it outranks the tier pick, the per-tier pins, and (like
    # ``codegen_cli_provider``) the payload ``model_override`` — operator pins
    # stay authoritative for the whole build. A junk slot string never breaks
    # a build: it logs a warning and falls back to tier routing. BILLING: a
    # ``*_cli`` slot runs on that CLI's subscription — spend the USD ledger
    # cannot see; an ``openrouter:`` slot bills the OpenRouter key like any pin.
    codegen_model_slot: str = ""
    repair_model_slot: str = ""
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
    # Run the headless build CLIs (codex exec / claude/kimi -p) WITHOUT the host's ambient MCP
    # servers. Otherwise every codegen call boots the user's whole ~/.claude MCP
    # fleet (Aruba, context7, playwright, ...) — pure per-build startup tax and a
    # sandboxing concern. True = isolated builds; flip to False only to give the
    # build agent your MCP tools on purpose.
    cli_disable_mcp: bool = True
    # Extra agentic codegen attempts when the first `claude -p` under-delivers
    # (produced only a placeholder/stub). Each retry re-runs with corrective
    # feedback. Fires ONLY on under-delivery, so a good first pass adds no time.
    agentic_retries: int = 1
    # No-progress window (seconds) for agentic codegen. Productive OpenRouter
    # write batches and streaming CLI events reset/extend this window, so it is
    # NOT a total full-app duration or quality cap. Non-streaming CLIs cannot
    # expose progress and therefore retain it as their bounded wait.
    agentic_build_timeout: int = 1800
    # Stall guard (seconds) for a streaming agentic session: if the agent emits
    # NO stream events for this long it has hung, so we kill it early instead of
    # burning the full build budget. A working `claude -p` emits message/tool
    # events far more often than this, so it never trips on real progress. 0
    # (or any negative value) uses ``agentic_build_timeout`` as the
    # inactivity/no-progress window — the same rule on both agentic paths
    # (see llm._resolved_agentic_idle_timeout).
    agentic_idle_timeout: int = 600
    # Liveness heartbeat (seconds) while a streaming agentic session runs. The
    # stream is where the only evidence of progress lives, and nothing surfaced
    # it: a measured build went 24 minutes between log lines while genuinely
    # working, which is indistinguishable from a hang to anyone watching. With
    # agentic_build_timeout at 1800 and best_of_n above 1, that silent window is
    # long enough that "is it running?" is a fair question. 0 disables.
    agentic_heartbeat_seconds: int = 60
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
    # Failover ladder bounds (win-rate sweep): at most this many fallback
    # candidates are appended after the primary misses (0 = primary only), and
    # an optional wall-clock deadline (seconds, 0 = disabled) caps one
    # resilient call end-to-end so retries x fallbacks x timeout cannot stack
    # into an unbounded stall.
    llm_max_fallbacks: int = Field(default=4, ge=0)
    llm_call_deadline_seconds: float = Field(default=0.0, ge=0.0)
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
    # Clean-room similar-project research. It inspects repository metadata plus
    # README/docs/manifests only and records ideas as optional backlog entries.
    github_similarity_research: bool = False
    github_similarity_max_repos: int = Field(default=8, ge=1, le=8)
    # Durable preflight/build DAG worker bound.
    build_graph_max_concurrency: int = Field(default=4, ge=1, le=16)

    # ---- Routing policy --------------------------------------------------
    free_only: bool = True
    # Claude is opt-in and defaults to UNCHECKED (the operator does not pay for
    # it): nothing resolves to Claude while this is true — not a claude_cli
    # backend, not a chain entry, not a codegen pin, not a council slot. It is a
    # hard fence, not a ranking tweak. Uncheck it in Settings (set false) only
    # when Claude is actually available and paid for.
    no_claude: bool = True
    model_evolution: bool = False  # opt-in: with auto_route, route via the learned ModelTournament router
    # Shared stage admission. Eight lets several full-app candidate/slice agents
    # progress together; provider-side retry/backoff still absorbs rate pressure.
    openrouter_max_concurrency: int = Field(default=8, ge=1)
    # Concurrent dispatches to any ONE local CLI provider. Fan-out multiplies
    # here (best_of_n x parallel_code_slices x per-file generation), and a CLI
    # slot is a 300MB-1GB agentic process tree whose subscription rate-limits per
    # account — so 32 concurrent `claude -p` produces thrash, not throughput.
    # This queues work, it never rejects it. 0 = unbounded (pre-2.1 behaviour).
    cli_max_concurrency: int = Field(default=2, ge=0)
    # Per-provider overrides, e.g. {"codex_cli": 1, "openrouter": 12}. Keys may
    # be "codex" or "codex_cli". Wins over the two class defaults above.
    provider_max_concurrency: dict[str, int] = Field(default_factory=dict)

    # ---- Optional cost/build ceilings ----------------------------------
    # Values <= 0 disable the corresponding guard.
    per_build_usd_cap: float = 0.0
    daily_usd_cap: float = 0.0
    daily_token_cap: int = 0
    autonomous_daily_build_cap: int = 0

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
    game_art_source: Literal["offline", "kenney", "replicate", "auto"] = "auto"
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
    # Empty -> a built-in default. The paid vision path is used only when the
    # OpenRouter backend itself was explicitly selected; auto soft-skips it.
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
    # failure blocks under release posture (or via blocking_gates /
    # ai_native_gates_verdict); under lab it records findings, dampens the
    # score, and feeds the repair loop. Soft-skips never block.
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
    # Advisory end-of-build WEB INTERACTION check (web stacks): serve the delivered
    # app in the isolated preview and drive ONE LLM-authored Playwright script
    # through ONE real user flow (click the nav, submit the main form), asserting
    # BOTH the UI surface (success state visible) and the backend surface (the
    # API/state endpoint reflects it) — the "renders but isn't wired" catch no
    # static/route gate can make. ADVISORY only: recorded to
    # manifest.extra["web_interact"], it NEVER flips the verdict; it soft-skips
    # ($0, decided before anything is served) without Playwright or a real
    # (non-stub) LLM backend.
    web_interact_check_enabled: bool = True
    # Deterministic CLI gate (python_cli family, wave-2 §3.6 tier): drive the
    # delivered main.py's command surface with bounded subprocess calls —
    # --help must work, every advertised subcommand's --help must work, and
    # (on subcommand CLIs) invalid input must be rejected with a nonzero exit
    # + stderr. ADVISORY like its siblings: recorded to
    # manifest.extra["cli_check"], one repair, never flips the verdict.
    cli_check_enabled: bool = True
    # Scripted interactive CLI playtest (Python CLI + native Swift aliases):
    # replay the generated project's root ``.skyn3t-cli-playtest.json`` contract
    # through a real PTY, including prompts, responses, transcript expectations,
    # and exit status. ADVISORY: records to manifest.extra["cli_playtest"], feeds
    # one bounded repair, and never flips the verdict. Default ON only in effect:
    # projects without the opt-in local contract soft-skip immediately.
    cli_playtest_enabled: bool = True
    # Deterministic deploy_check gate (Ship pillar): after a REAL deploy, re-run
    # the liveness/contract probes against the LIVE url (not localhost). Opt-in
    # (default OFF) — it only applies once something is actually deployed. It does
    # not rewrite build proof/verdict, but a non-positive check blocks that remote
    # attempt from becoming the manifest's active live_url pointer.
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
    # High-confidence UI proof is blocking in real runs: web apps must launch
    # in the Docker-only preview and pass Playwright; React Native apps must
    # provide and pass Maestro flows. Tests disable this globally and exercise
    # it through explicit seam tests.
    proof_ladder_required: bool = True
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

    # Debaters for `skyn3t debate`, as comma-separated provider:model slots
    # (same grammar as moa_advisors). Empty means every debater resolves to the
    # SAME routed model — one model arguing with itself, whose "win" is then
    # recorded unopposed into the ModelTournament. Set this to make it a real
    # ensemble, e.g. "codex_cli,claude_cli,kimi_cli".
    debate_slots: str = ""

    # ---- Mixture-of-Agents advisory council -------------------------------
    # N tool-free advisor models read the brief + stack + plan and hand private
    # engineering guidance to the ONE agent that actually writes the app (the
    # aggregator, i.e. the code agent). Advisors never write files, never score,
    # never gate — this is a capability layer, not a check. Adapted from
    # NousResearch/hermes-agent (MIT); see docs/MOA.md and CREDITS.md.
    # ON by default. The council is what makes a build multi-model instead of
    # whatever one CLI happens to be routed, so it is the default posture, not
    # an add-on. It stays inert until moa_advisors is non-empty AND the backend
    # is not stub, so the offline test suite and a keyless checkout still cost
    # nothing. Clearing moa_advisors is the ordinary way to turn it off;
    # SKYN3T_MOA_ENABLED=0 is the master switch.
    moa_enabled: bool = True
    # Advisor slots as comma-separated "provider:model" (model optional, empty =
    # that CLI's own default). Providers may DIFFER per slot — that is the whole
    # point. Addressable backends: codex_cli, claude_cli, kimi_cli, openrouter
    # (and copilot_cli, supported but not in the default chain). Prefer the BARE
    # form — no ":model" — so each CLI uses its own configured default:
    #   "codex_cli,claude_cli,kimi_cli"
    # A bare slot also sidesteps id churn. "kimi_cli:k3" in particular does NOT
    # work: that CLI needs the fully-qualified "kimi-code/k3", which its own
    # config.toml already sets as the default, so bare "kimi_cli" is correct.
    # Pin a model only when you mean it, e.g. "claude_cli:sonnet".
    # Empty => the council is off even when moa_enabled.
    # Defaults to Kimi + Copilot + one hosted OpenRouter model: a genuinely
    # multi-model council with no Claude. Claude is deliberately NOT in the
    # default: nothing Claude-powered runs unless the operator selects it —
    # add it back with
    #   SKYN3T_MOA_ADVISORS="claude_cli,kimi_cli"
    # (codex_cli is left out because auto routes codegen to Codex first, so a
    # codex advisor would be the acting model reviewing itself). A slot whose
    # CLI is not installed is recorded as a failed advisor and the build
    # proceeds on the survivors; if ALL fail the codegen prompt is
    # byte-identical to a council-off build, so this default costs nothing on a
    # machine without them. Set SKYN3T_MOA_ADVISORS="" to turn the council off.
    moa_advisors: str = "kimi_cli,copilot_cli,openrouter"
    # Concurrent advisor calls. Matches the ceiling code_agent already uses for
    # nested CLI children; per-provider limits still apply underneath.
    moa_max_concurrency: int = Field(default=4, ge=1, le=8)
    # Wall-clock ceiling per advisor. A slow advisor is dropped AT THIS DEADLINE
    # — but the council IS awaited inline before codegen, so it can delay a
    # build by up to ceil(N_advisors / moa_max_concurrency) * this value. The
    # earlier claim that advisors "never extend the critical path" was wrong.
    # 60s, not 180s: a tool-free advisory of <=400 words does not need three
    # minutes, and now that the council is on by default that delay is paid by
    # every build.
    moa_advisor_timeout: int = Field(default=60, ge=10)
    # Output cap per advisor (binds on OpenRouter; CLI backends are governed by
    # the byte budget below, since complete() cannot pass max_tokens to a CLI).
    moa_advisor_max_tokens: int = Field(default=1200, ge=128)
    # Default reasoning effort for advisor slots that don't pin their own with
    # the "@effort" suffix (e.g. "claude_cli:sonnet@high" — see
    # adapters/model_slot.py). One of low|medium|high; anything else falls back
    # to "medium" with a logged warning at the council's single resolution
    # chokepoint (resolve_advisor_effort). Mapping: an OpenRouter advisor call
    # carries {"reasoning": {"effort": X}} on the request — the API ignores it
    # for models without a reasoning mode, so it can never break a call — while
    # the CLI invocation templates (_CLI_COMMANDS) carry no effort flag, so for
    # CLI advisors effort is a deliberate no-op, logged once per provider.
    moa_advisor_effort: str = "medium"
    # Hard per-advisor byte budget for the assembled guidance block. Prompt bloat
    # is the real hazard here: an oversized codegen prompt slows the agentic CLI
    # enough to blow agentic_build_timeout and ship a stub.
    moa_advisor_block_bytes: int = Field(default=3000, ge=500)
    # Opt-in JSONL trace: one line per council run under logs_dir/moa/.
    moa_trace_enabled: bool = False

    # ---- Gate posture ----------------------------------------------------
    # Whether a gate's finding BLOCKS the verdict, separately from whether the
    # gate RUNS (that stays each gate's own *_enabled flag, above). "lab"
    # (default): only proof that the DELIVERY IS BROKEN blocks; heuristics,
    # taste rules, environment-dependent probes and policy preferences record
    # their finding, dampen the score, feed the fix loop, and let the build
    # complete. "release": every gate blocks (2.0's historical behaviour).
    # A gate that COULD NOT RUN never blocks in EITHER posture — that is
    # GateVerdict's `skipped` contract applied to every gate, including the
    # ones that never adopted it. See skyn3t/studio/gate_posture.py.
    build_posture: Literal["lab", "release"] = "lab"
    # Escape hatch: comma-separated gate names that block regardless of posture
    # (for example "security,proof_ladder"). Empty -> the posture decides.
    blocking_gates: str = ""

    # ---- Autonomy --------------------------------------------------------
    autonomous_builds: bool = False
    autonomous_learning: bool = True
    approval_gates: bool = True
    cortex_auto_approve_safe: bool = True
    # Personal-lab profile: remove repetitive approval and budget friction for
    # local generation/research while retaining every proof and quality gate.
    lab_autonomy: bool = False
    # One plain-language control for the local recovery / learning queue. It
    # never enables remote push or deployment; it only selects local work.
    lab_autopilot: bool = False
    # Cortex can author narrowly scoped self-improvement candidates in an
    # isolated git worktree. The engine never pushes. Local main auto-merge is
    # a separate explicit consent and only occurs after all blocking gates pass.
    cortex_candidates_enabled: bool = True
    cortex_candidate_auto_merge: bool = False
    cortex_candidate_merge_strategy: Literal["ff-only", "squash"] = "ff-only"
    cortex_candidate_timeout: int = Field(default=1800, ge=60, le=3600)
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
    execution_backend: Literal["auto", "docker", "inline"] = "auto"
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
                ("netlify", self.netlify_auth_token),
                ("railway", self.railway_token),
                ("render", self.render_api_key),
            )
            if tok
        }

    @property
    def has_any_llm(self) -> bool:
        """Whether a CONSUMABLE hosted-LLM credential is configured.

        Only the OpenRouter key counts: no backend consumes the
        anthropic/openai/kimi keys, so including them made ``/api/status``
        claim LLM availability while generation stayed on the offline stub.
        (Local CLI availability is reported separately by the runtime — see
        ``web/deps.py`` status(), which ORs in the resolved backend.)
        The ``openrouter_enabled`` disconnect flag suppresses availability
        just as it suppresses key resolution (mirror of ``llm.openrouter_key``):
        a disabled key cannot generate, so it must not report as available.
        """
        return bool(self.openrouter_api_key) and bool(self.openrouter_enabled)


@lru_cache
def get_settings() -> Settings:
    """Cached singleton accessor."""
    return Settings()
