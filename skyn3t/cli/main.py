"""SkyN3t 2.0 command-line interface.

A Typer app that drives the autonomous app factory from the terminal:

  * ``skyn3t start``           boot the spine, register every available agent,
                              optionally launch the web control plane
  * ``skyn3t doctor``         readiness report (python, deps, db, llm, sandbox,
                              projects dir) rendered as a rich table
  * ``skyn3t studio build``   run a brief -> app build end to end
  * ``skyn3t studio approve`` / ``reject``  decide a gated build (best effort)
  * ``skyn3t project list``   show recent builds from memory
  * ``skyn3t snapshot``       save spine state (event history) to disk
  * ``skyn3t domain ingest``  ingest a path or URL into the knowledge base

Design notes
------------
* Every command does its heavy imports **lazily, inside the command body** so
  the CLI loads fast and tolerates missing optional packages (design rule #6:
  degrade, don't crash). Importing this module has zero side effects.
* Agent construction is signature-aware: each agent class has a slightly
  different ``__init__`` (some take ``llm``, some ``llm_client``, some
  ``memory``), so we introspect the signature and pass only what it accepts.
* Nothing here writes to disk or touches the network at import time.
"""

from __future__ import annotations

import asyncio
import html
import inspect
import json
import re
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(
    name="skyn3t",
    help="SkyN3t 2.0 — autonomous multi-agent app factory.",
    no_args_is_help=True,
    add_completion=False,
)

studio_app = typer.Typer(help="Run and steer brief->app builds.", no_args_is_help=True)
project_app = typer.Typer(help="Inspect delivered projects / builds.", no_args_is_help=True)
domain_app = typer.Typer(help="Ingest external knowledge (RAG corpus).", no_args_is_help=True)
app.add_typer(studio_app, name="studio")
app.add_typer(project_app, name="project")
app.add_typer(domain_app, name="domain")


# ---------------------------------------------------------------------------
# Console helpers — fall back to plain ``print`` when ``rich`` is absent.
# ---------------------------------------------------------------------------
def _console() -> Any:
    try:
        from rich.console import Console

        return Console()
    except Exception:  # noqa: BLE001 - rich is optional
        class _Plain:
            def print(self, *args: Any, **kwargs: Any) -> None:
                cleaned = [_strip_markup(str(a)) for a in args]
                print(*cleaned)

        return _Plain()


def _strip_markup(text: str) -> str:
    """Remove ``[style]...[/style]`` markup for the no-rich fallback."""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]" and depth:
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _table(title: str, columns: list[str]) -> Any:
    """Return a rich Table or a tiny text-table shim."""
    try:
        from rich.table import Table

        table = Table(title=title)
        for col in columns:
            table.add_column(col)
        return table
    except Exception:  # noqa: BLE001
        class _TextTable:
            def __init__(self, title: str, columns: list[str]) -> None:
                self.title = title
                self.columns = columns
                self.rows: list[list[str]] = []

            def add_row(self, *cells: str) -> None:
                self.rows.append([_strip_markup(str(c)) for c in cells])

            def __str__(self) -> str:
                lines = [self.title, "  ".join(self.columns), "-" * 40]
                for row in self.rows:
                    lines.append("  ".join(row))
                return "\n".join(lines)

        return _TextTable(title, columns)


def _ok(flag: bool) -> str:
    return "[green]OK[/green]" if flag else "[red]MISSING[/red]"


# ---------------------------------------------------------------------------
# Agent registry — (module path, class name). Capabilities follow the canonical
# stage vocabulary; the runner matches on capability, not class.
# ---------------------------------------------------------------------------
_AGENT_MODULES: tuple[tuple[str, str], ...] = (
    ("skyn3t.agents.brainstorm", "BrainstormAgent"),
    ("skyn3t.agents.research_agent", "ResearchAgent"),
    ("skyn3t.agents.architect", "ArchitectAgent"),
    ("skyn3t.agents.designer", "DesignerAgent"),
    ("skyn3t.agents.code_agent", "CodeAgent"),
    ("skyn3t.agents.code_improver", "CodeImproverAgent"),
    ("skyn3t.agents.reviewer", "ReviewerAgent"),
    ("skyn3t.agents.critic", "CriticAgent"),
    ("skyn3t.agents.writer", "WriterAgent"),
    ("skyn3t.agents.contract_verifier", "ContractVerifierAgent"),
    ("skyn3t.agents.build_verifier", "BuildVerifierAgent"),
    ("skyn3t.agents.boot_verifier", "BootVerifierAgent"),
    ("skyn3t.agents.consistency_reviewer", "ConsistencyReviewerAgent"),
    ("skyn3t.agents.integration_verifier", "IntegrationVerifierAgent"),
    ("skyn3t.agents.test_author", "TestAuthorAgent"),
    ("skyn3t.agents.packaging_agent", "PackagingAgent"),
    ("skyn3t.agents.deploy_agent", "DeployAgent"),
    ("skyn3t.agents.browser_agent", "BrowserAgent"),
    ("skyn3t.agents.github_explorer", "GithubExplorer"),
    ("skyn3t.agents.github_ingestor", "GithubIngestor"),
)


def _construct_agent(
    cls: Any, *, event_bus: Any, llm: Any, memory: Any, name: str | None = None
) -> Any:
    """Build an agent, passing only kwargs its ``__init__`` actually accepts."""
    sig = inspect.signature(cls.__init__)
    params = sig.parameters
    kwargs: dict[str, Any] = {}
    if name is not None and "name" in params:
        kwargs["name"] = name
    if "event_bus" in params:
        kwargs["event_bus"] = event_bus
    if "llm" in params:
        kwargs["llm"] = llm
    if "llm_client" in params:
        kwargs["llm_client"] = llm
    if "memory" in params:
        kwargs["memory"] = memory
    return cls(**kwargs)


def build_agents(*, event_bus: Any, llm: Any = None, memory: Any = None) -> list[Any]:
    """Instantiate every importable agent. Missing/broken agents are skipped."""
    agents: list[Any] = []
    for module_path, class_name in _AGENT_MODULES:
        try:
            module = __import__(module_path, fromlist=[class_name])
            cls = getattr(module, class_name)
            agents.append(_construct_agent(cls, event_bus=event_bus, llm=llm, memory=memory))
        except Exception:  # noqa: BLE001 - a missing dep must not break the rest
            continue
    return agents


# ---------------------------------------------------------------------------
# Spine assembly — shared by ``start`` and ``studio build``.
# ---------------------------------------------------------------------------
async def _assemble_spine(*, with_memory: bool = True, event_bus: Any | None = None) -> dict[str, Any]:
    """Wire event bus, orchestrator, llm, router, memory, and agents.

    Returns a dict of collaborators. Every piece degrades independently.
    Pass ``event_bus`` to share one bus with the web layer's WebSocket bridge.
    """
    from skyn3t.config.settings import get_settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator

    settings = get_settings()
    event_bus = event_bus or EventBus()

    llm = None
    router = None
    try:
        from skyn3t.adapters.llm import LLMClient

        llm = LLMClient(settings)
        router = getattr(llm, "router", None)
    except Exception:  # noqa: BLE001
        llm = None

    memory = None
    if with_memory:
        try:
            from skyn3t.memory.store import MemoryStore

            memory = MemoryStore(settings)
            await memory.init_db()
        except Exception:  # noqa: BLE001
            memory = None

    persist = getattr(memory, "save_task", None) if memory is not None else None
    orchestrator = Orchestrator(
        event_bus,
        max_concurrency=settings.openrouter_max_concurrency,
        persist=persist,
    )

    agents = build_agents(event_bus=event_bus, llm=llm, memory=memory)
    registered = 0
    for agent in agents:
        try:
            await orchestrator.register(agent)
            registered += 1
        except Exception:  # noqa: BLE001
            continue

    return {
        "settings": settings,
        "event_bus": event_bus,
        "orchestrator": orchestrator,
        "llm": llm,
        "router": router,
        "memory": memory,
        "agents_registered": registered,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@app.command()
def start(
    web: bool = typer.Option(False, "--web", help="Also launch the web control plane."),
    host: str = typer.Option("", "--host", help="Web bind host (default from settings)."),
    port: int = typer.Option(0, "--port", help="Web bind port (default from settings)."),
) -> None:
    """Boot the orchestrator, register available agents, optionally run the web UI."""
    console = _console()

    # Web path: assemble + serve on one event loop (studio wired into the app).
    if web:
        try:
            asyncio.run(_serve_web(console, host, port))
        except KeyboardInterrupt:  # pragma: no cover - manual stop
            console.print("Stopped.")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Web server unavailable:[/red] {exc}")
            console.print("Install with: [cyan]pip install -e \".[web]\"[/cyan]")
            raise typer.Exit(code=1)
        return

    spine = asyncio.run(_assemble_spine())
    settings = spine["settings"]
    orchestrator = spine["orchestrator"]

    console.print(
        f"[bold]{settings.app_name} {settings.version}[/bold] booted — "
        f"[green]{spine['agents_registered']}[/green] agents registered."
    )
    table = _table("Registered agents", ["agent", "type", "provider"])
    for name, agent in orchestrator.agents.items():
        table.add_row(
            name,
            getattr(agent, "agent_type", ""),
            getattr(agent, "provider", ""),
        )
    console.print(table)
    console.print(
        "Spine is ready. Run [cyan]skyn3t studio build \"<brief>\"[/cyan] to build, "
        "or pass [cyan]--web[/cyan] to start the control plane."
    )


@app.command()
def doctor() -> None:
    """Print a readiness report: python, deps, db, llm, sandbox, projects dir."""
    console = _console()
    import platform
    import sys

    from skyn3t.config.settings import get_settings

    settings = get_settings()
    table = _table("SkyN3t 2.0 doctor", ["check", "status", "detail"])

    # Python version (need 3.11+).
    py_ok = sys.version_info >= (3, 11)
    table.add_row(
        "python", _ok(py_ok), f"{platform.python_version()} (need >= 3.11)"
    )

    # Core + optional dependencies.
    core_deps = ["typer", "pydantic", "pydantic_settings", "structlog", "sqlalchemy", "aiosqlite"]
    optional_deps = [
        "rich", "httpx", "fastapi", "uvicorn", "chromadb",
        "sentence_transformers", "docker", "prometheus_client",
        "playwright", "tree_sitter",
    ]
    for dep in core_deps:
        present = _has_module(dep)
        table.add_row(f"dep:{dep}", _ok(present), "core")
    for dep in optional_deps:
        present = _has_module(dep)
        status = _ok(present) if present else "[yellow]optional[/yellow]"
        table.add_row(f"dep:{dep}", status, "optional (degrades)")

    # Database init.
    db_detail, db_ok = _check_db(settings)
    table.add_row("database", _ok(db_ok), db_detail)

    # LLM backend.
    llm_backend, llm_ok = _check_llm(settings)
    status = _ok(llm_ok) if llm_ok else "[yellow]stub[/yellow]"
    table.add_row("llm backend", status, llm_backend)

    # Sandbox backend.
    sandbox_detail = _check_sandbox(settings)
    table.add_row("sandbox", "[green]OK[/green]", sandbox_detail)

    # Projects dir writable.
    proj_ok, proj_detail = _check_writable(settings.projects_dir)
    table.add_row("projects dir", _ok(proj_ok), proj_detail)

    console.print(table)
    console.print(
        f"policy: free_only={settings.free_only} no_claude={settings.no_claude} "
        f"approval_gates={settings.approval_gates} has_any_llm={settings.has_any_llm}"
    )


@studio_app.command("build")
def studio_build(
    brief: str = typer.Argument(..., help="What to build, in plain English."),
    best_of: int = typer.Option(0, "--best-of", "-n", help="Best-of-N code trajectories."),
    no_critic: bool = typer.Option(False, "--no-critic", help="Disable the adversarial critic gate."),
    slug: str = typer.Option("", "--slug", help="Override the project slug."),
    stack: str = typer.Option("", "--stack", help="Pin the stack: react|react_native|fastapi|static|python|express."),
) -> None:
    """Run a build end to end and print the result + artifact path."""
    console = _console()
    outcome = asyncio.run(_run_build(brief, best_of=best_of, no_critic=no_critic, slug=slug, stack=stack))
    if outcome is None:
        console.print("[red]Build pipeline unavailable (studio package missing).[/red]")
        raise typer.Exit(code=1)

    verdict_color = "green" if outcome.get("verdict") == "go" else "red"
    table = _table("Build result", ["field", "value"])
    table.add_row("build_id", str(outcome.get("build_id", "")))
    table.add_row("slug", str(outcome.get("slug", "")))
    table.add_row("stack", str(outcome.get("stack", "")))
    table.add_row("status", str(outcome.get("status", "")))
    table.add_row("verdict", f"[{verdict_color}]{outcome.get('verdict', '')}[/{verdict_color}]")
    table.add_row("score", str(outcome.get("score", "")))
    table.add_row("files", str(len(outcome.get("files", []))))
    table.add_row("cost_usd", str(outcome.get("cost_usd", 0.0)))
    table.add_row("artifact", str(outcome.get("project_dir", "")))
    console.print(table)
    if outcome.get("status") != "completed":
        raise typer.Exit(code=2)


async def _run_debate(question: str, settings: Any | None = None) -> Any:
    """Run a multi-model debate, gated by ``debate_enabled``, feeding the
    ModelTournament that the learned router reads. Returns ``None`` if the LLM
    stack is unavailable."""
    try:
        from skyn3t.adapters.llm import LLMClient
        from skyn3t.config.settings import get_settings
        from skyn3t.core.events import EventBus
        from skyn3t.intelligence.debate import run_debate
        from skyn3t.intelligence.model_tournament import ModelTournament
    except Exception:  # noqa: BLE001 - optional stack
        return None
    settings = settings or get_settings()
    llm = LLMClient(settings)
    tournament = ModelTournament(settings.data_dir / "model_tournament.json")
    return await run_debate(
        llm,
        question,
        enabled=bool(settings.debate_enabled),
        tournament=tournament,
        event_bus=EventBus(),
    )


@app.command()
def debate(
    question: str = typer.Argument(..., help="The question to debate across models."),
) -> None:
    """Multi-model debate: propose -> cross-examine -> vote -> synthesise.

    Gated by SKYN3T_DEBATE_ENABLED: off (default) is a single cheap completion;
    on runs several models and records the winner into the ModelTournament that
    feeds the learned router. Degrades deterministically on the stub backend.
    """
    console = _console()
    result = asyncio.run(_run_debate(question))
    if result is None:
        console.print("[red]LLM stack unavailable — cannot debate.[/red]")
        raise typer.Exit(code=1)
    mode = "full debate" if result.enabled else "single completion (SKYN3T_DEBATE_ENABLED=0)"
    console.print(f"[cyan]mode[/cyan]: {mode}")
    if result.winner is not None:
        console.print(f"[cyan]winner model[/cyan]: {result.winner.model}")
    console.print("\n[bold]Synthesis[/bold]\n")
    console.print(result.synthesis or "(empty)")


def _build_intelligence(settings: Any, event_bus: Any, memory: Any) -> tuple[Any, Any, Any, Any]:
    """Construct the self-improvement layer (learning loop, pattern board, skills, rag).

    Each piece is guarded — a missing module just yields ``None`` and the runner
    falls back to the core MemoryStore lesson loop. The RAG engine is SHARED:
    the ExperienceIngestor writes build outcomes into it and the studio reads
    recall out of it, so the system learns from its own past builds (and from
    GitHub repos ingested via `domain ingest`).
    """
    learning = patterns = skills = rag = None
    try:
        from skyn3t.intelligence.learning_loop import LearningLoop
        learning = LearningLoop(store=memory, event_bus=event_bus)
    except Exception:  # noqa: BLE001
        pass
    try:
        from skyn3t.intelligence.build_patterns import BuildPatternBoard
        patterns = BuildPatternBoard(settings.data_dir / "build_patterns.json")
    except Exception:  # noqa: BLE001
        pass
    try:
        from skyn3t.intelligence.skill_library import SkillLibrary, seed_default_skills
        skills = SkillLibrary(settings.data_dir / "skills")
        seed_default_skills(skills)  # starter skills so builds have advice from day 1
    except Exception:  # noqa: BLE001
        pass
    # Shared persistent RAG + live experience ingestion. The ingestor subscribes
    # to build/task events and writes outcomes into the SAME store the studio
    # recalls from — closing the learn-from-experience loop into build prompts.
    try:
        from skyn3t.rag.rag_engine import RagEngine

        rag = RagEngine(persist_path=settings.vector_db_path)
        from skyn3t.memory.ingestor import ExperienceIngestor

        ExperienceIngestor(event_bus, rag_engine=rag).start()
    except Exception:  # noqa: BLE001
        rag = None
    return learning, patterns, skills, rag


def _build_observability(settings: Any, llm: Any) -> tuple[Any, Any]:
    """Construct a cost tracker + budget guard for a build loop (guarded)."""
    cost_tracker = budget_guard = None
    try:
        from skyn3t.observability.cost_tracker import CostTracker
        if llm is not None:
            cost_tracker = CostTracker.from_llm(llm, settings)
    except Exception:  # noqa: BLE001
        pass
    try:
        from skyn3t.self_healing.budget import BudgetGuard
        budget_guard = BudgetGuard(settings=settings, budget=getattr(llm, "budget", None))
    except Exception:  # noqa: BLE001
        pass
    return cost_tracker, budget_guard


async def _run_build(brief: str, *, best_of: int, no_critic: bool, slug: str, stack: str = "") -> dict[str, Any] | None:
    try:
        from skyn3t.studio.runner import StudioRunner
    except Exception:  # noqa: BLE001
        return None

    spine = await _assemble_spine()
    settings = spine["settings"]
    if no_critic:
        # Per-run override on a *copy* so we never mutate the cached
        # get_settings() singleton (which would silently disable the critic for
        # every subsequent build / reader in this process).
        try:
            settings = settings.model_copy(update={"critic_enabled": False})
        except Exception:  # noqa: BLE001 - degrade, don't crash
            pass

    learning, patterns, skills, rag = _build_intelligence(settings, spine["event_bus"], spine["memory"])
    cost_tracker, budget_guard = _build_observability(settings, spine["llm"])
    runner = StudioRunner(
        spine["event_bus"],
        spine["orchestrator"],
        settings=settings,
        memory=spine["memory"],
        learning=learning,
        patterns=patterns,
        skills=skills,
        cost_tracker=cost_tracker,
        budget_guard=budget_guard,
        rag=rag,
    )
    extra: dict[str, Any] = {}
    if best_of and best_of > 1:
        extra["best_of_n"] = best_of
    if stack:
        extra["stack"] = stack
    outcome = await runner.start(brief, slug=slug or None, extra=extra)

    # --- C: one bounded, gated learning tick (no loop, no autonomous builds) ---
    # The web path runs the cortex continuously; the CLI 'studio build' path
    # never did, so MetaTick/SelfTuning never fired and learned tuning never
    # carried forward. Run ONE synchronous observe tick (gated by
    # autonomous_learning), persist any SAFE tuning, then detach. Best-effort.
    try:
        if settings.autonomous_learning:
            from skyn3t.cortex.bootstrap import build_cortex
            from skyn3t.cortex.meta_tick import MetaTick
            from skyn3t.cortex.tuning_store import PERSISTABLE_TUNING, persist_overrides
            from skyn3t.memory.meta_agent import MetaAgent
            from skyn3t.memory.tuner import SelfTuningEngine

            bus, mem = spine["event_bus"], spine["memory"]
            cortex = build_cortex(
                bus, settings,
                orchestrator=spine["orchestrator"], memory=mem, llm=spine["llm"],
            )
            # The SelfTuningEngine in the cortex is already subscribed to
            # INSIGHT_PUBLISHED. We deliberately do NOT call cortex.start() — one
            # synchronous tick, no background loops, no autonomous builds.
            meta_agent = MetaAgent(bus, store=mem) if mem is not None else None
            await MetaTick(cortex, bus, settings, meta_agent=meta_agent).tick_once()

            # Read applied tuning from the tuner's history (the source the
            # INSIGHT->SelfTuning path populates), then persist only SAFE keys.
            tuner = next(
                (c for c in cortex._components if isinstance(c, SelfTuningEngine)), None
            )
            applied: dict[str, Any] = {}
            if tuner is not None:
                for ch in tuner.history:
                    if ch.key in PERSISTABLE_TUNING:
                        applied[ch.key] = ch.new
            if applied:
                persist_overrides(settings.data_dir, applied)
            await cortex.stop()
    except Exception:  # noqa: BLE001 - learning is best-effort; never fail a build
        pass

    return outcome.to_dict()


async def _run_improve(project: str, *, goal: str) -> dict[str, Any] | None:
    try:
        from skyn3t.studio.improve import ImproveEngine
    except Exception:  # noqa: BLE001 - optional studio package
        return None
    spine = await _assemble_spine()
    settings = spine["settings"]
    _learning, _patterns, skills, rag = _build_intelligence(settings, spine["event_bus"], spine["memory"])
    engine = ImproveEngine(
        spine["event_bus"], spine["orchestrator"],
        settings=settings, memory=spine["memory"], skills=skills, rag=rag,
    )
    outcome = await engine.improve(project, goal)
    return outcome.to_dict()


@studio_app.command("serve")
def studio_serve(
    project: str = typer.Argument(..., help="Project slug (under Projects/) or an absolute path."),
    port: int = typer.Option(0, "--port", "-p", help="Preferred port (0 = auto)."),
) -> None:
    """Run a generated project as a live local server and print its URL."""
    import time as _time
    from pathlib import Path as _Path

    from skyn3t.config.settings import get_settings
    from skyn3t.studio.app_runner import AppRunner

    console = _console()
    s = get_settings()
    cand = _Path(project)
    pdir = cand if cand.is_absolute() else (s.projects_dir / project)
    man = None
    try:
        from skyn3t.studio.manifest import BuildManifest
        man = BuildManifest.load(pdir)
    except Exception:  # noqa: BLE001
        man = None
    stack = man.stack if man else ""
    runner = AppRunner()
    app = asyncio.run(runner.start(pdir, stack, port=port or None))
    if app.status == "no_preview":
        console.print(f"[yellow]No live preview[/yellow] for {pdir} (not a web/site project).")
        raise typer.Exit(code=1)
    if app.status != "running":
        console.print(f"[red]Failed to start[/red]: {app.detail.get('log_tail', '')[-400:]}")
        raise typer.Exit(code=2)
    console.print(f"[green]Serving[/green] {pdir.name} at [cyan]{app.url}[/cyan] (pid {app.pid}). "
                  "Press Ctrl+C to stop.")
    try:
        while True:
            _time.sleep(1)
    except KeyboardInterrupt:
        runner.stop(app)
        console.print("\n[dim]stopped.[/dim]")


@studio_app.command("shoot")
def studio_shoot(
    url: str = typer.Argument(..., help="URL to screenshot (e.g. http://127.0.0.1:8088/)."),
    out: str = typer.Option("", "--out", "-o", help="Output PNG path (default: a temp file)."),
) -> None:
    """Capture a screenshot of a running app (needs Playwright)."""
    import os
    import tempfile as _tempfile

    from skyn3t.studio.visual_check import playwright_available, screenshot

    console = _console()
    if not playwright_available():
        console.print("[yellow]Playwright not installed.[/yellow] "
                      "Run [cyan]pip install playwright && playwright install chromium[/cyan] to enable screenshots.")
        raise typer.Exit(code=1)
    if out:
        out_path = out
    else:
        _fd, out_path = _tempfile.mkstemp(prefix="skyn3t-shot-", suffix=".png")
        os.close(_fd)
    result = screenshot(url, out_path)
    if result is None:
        console.print(f"[red]Screenshot failed[/red] for {url} (page didn't load or no browser binary).")
        raise typer.Exit(code=2)
    console.print(f"[green]Saved[/green] screenshot to [cyan]{result}[/cyan]")


@studio_app.command("improve")
def studio_improve(
    project: str = typer.Argument(..., help="Project slug (under Projects/) or an absolute path."),
    goal: str = typer.Option(..., "--goal", "-g", help="What to add/change, in plain English."),
) -> None:
    """Improve an already-built project toward a goal (audit -> edit -> verify -> deliver)."""
    console = _console()
    outcome = asyncio.run(_run_improve(project, goal=goal))
    if outcome is None:
        console.print("[red]Improve pipeline unavailable (studio package missing).[/red]")
        raise typer.Exit(code=1)
    color = "green" if outcome.get("proof_passed") else "yellow"
    table = _table("Improve result", ["field", "value"])
    table.add_row("slug", str(outcome.get("slug", "")))
    table.add_row("stack", str(outcome.get("stack", "")))
    table.add_row("goal", str(outcome.get("goal", "")))
    table.add_row("status", str(outcome.get("status", "")))
    table.add_row("files_changed", str(len(outcome.get("files_changed", []))))
    table.add_row("proof", f"[{color}]{'passed' if outcome.get('proof_passed') else 'check'}[/{color}]")
    table.add_row("score", str(outcome.get("score", "")))
    table.add_row("project", str(outcome.get("project_dir", "")))
    console.print(table)
    if outcome.get("status") != "completed":
        raise typer.Exit(code=2)


async def assemble_app_state(event_bus: Any | None = None) -> Any:
    """Build a fully-wired web ``AppState`` (spine + studio + intelligence).

    Assembled on the *current* event loop so all async primitives bind to the
    serving loop — call this from inside the uvicorn loop, not via a separate
    ``asyncio.run``.
    """
    from skyn3t.web.deps import AppState

    spine = await _assemble_spine(event_bus=event_bus)
    settings = spine["settings"]
    bus = spine["event_bus"]

    # Recovery: restore prior state on boot, then announce (best-effort).
    try:
        from skyn3t.persistence.recovery import RecoveryManager

        await RecoveryManager().restore_and_announce(bus)
    except Exception:  # noqa: BLE001
        pass

    studio = None
    rag = None  # hoisted: shared with the cortex block below (NameError-safe if studio init fails)
    skills = None  # hoisted likewise: threaded into the cortex for skill distillation
    try:
        from skyn3t.studio.runner import StudioRunner

        learning, patterns, skills, rag = _build_intelligence(settings, bus, spine["memory"])
        cost_tracker, budget_guard = _build_observability(settings, spine["llm"])
        studio = StudioRunner(
            bus,
            spine["orchestrator"],
            settings=settings,
            memory=spine["memory"],
            learning=learning,
            patterns=patterns,
            skills=skills,
            cost_tracker=cost_tracker,
            budget_guard=budget_guard,
            rag=rag,
        )
    except Exception:  # noqa: BLE001 - dashboard still works read-only
        studio = None

    # Cortex autonomy heartbeat (gated). build_cortex wires the MetaTick +
    # SelfTuningEngine; start() spawns the component loops on this loop.
    cortex = None
    try:
        if settings.autonomous_learning or settings.autonomous_builds:
            from skyn3t.cortex.bootstrap import build_cortex

            cortex = build_cortex(
                bus, settings,
                orchestrator=spine["orchestrator"], memory=spine["memory"], llm=spine["llm"],
                rag=rag,  # cortex ingestion writes into the same corpus studio recalls from
                skills=skills,  # ingested repos also distill into advisory skills
            )
            await cortex.start()
    except Exception:  # noqa: BLE001 - autonomy is optional
        cortex = None

    # Messaging service: outbound build notifications wired immediately; the
    # inbound bot listener is opt-in (started from the dashboard).
    messaging = None
    try:
        from skyn3t.integrations.service import MessagingService

        messaging = MessagingService(bus, settings, studio=studio)
    except Exception:  # noqa: BLE001
        messaging = None

    return AppState(
        settings=settings,
        event_bus=bus,
        orchestrator=spine["orchestrator"],
        memory=spine["memory"],
        studio=studio,
        llm_client=spine["llm"],
        router=spine["router"],
        cortex=cortex,
        skills=getattr(studio, "skills", None),
        messaging=messaging,
    )


async def _serve_web(console: Any, host: str, port: int) -> None:
    """Assemble a wired app and serve it — assembly + serving share one loop."""
    import uvicorn

    from skyn3t.web import app as web_app

    if not web_app.fastapi_available():
        raise RuntimeError("fastapi/uvicorn not installed")

    state = await assemble_app_state()
    application = web_app.create_app(state=state)
    settings = state.settings
    bind_host = host or settings.host
    bind_port = port or settings.port
    console.print(
        f"[bold]{settings.app_name} {settings.version}[/bold] — "
        f"[green]{len(state.orchestrator.agents)}[/green] agents · studio "
        f"[green]{'wired' if state.studio else 'unavailable'}[/green]"
    )
    console.print(f"Control plane on [cyan]http://{bind_host}:{bind_port}[/cyan]  (Ctrl-C to stop)")
    config = uvicorn.Config(application, host=bind_host, port=bind_port, log_level="info")
    await uvicorn.Server(config).serve()


@studio_app.command("approve")
def studio_approve(build_id: str = typer.Argument(..., help="Build id to approve.")) -> None:
    """Approve a pending gated build (records the decision as an event)."""
    _decide_build(build_id, approve=True)


@studio_app.command("reject")
def studio_reject(build_id: str = typer.Argument(..., help="Build id to reject.")) -> None:
    """Reject a pending gated build (records the decision as an event)."""
    _decide_build(build_id, approve=False)


def _decide_build(build_id: str, *, approve: bool) -> None:
    """Deliver an approval/rejection to the *running* control plane.

    A gated build is blocked inside a live process waiting on its in-process
    ``approval_gate``. The only way the CLI can unblock it is by reaching that
    process, so we POST the decision to the running web control plane
    (``/studio/approve``), which mutates the durable build record and resolves
    the gate on the shared spine.

    Emitting onto a throwaway in-process ``EventBus`` (the old behavior) would
    silently discard the decision — no subscriber, no persistence — yet still
    print success. We instead only report success on a confirmed 2xx, and
    surface a clear error when no live build process is reachable.
    """
    console = _console()

    async def _post() -> tuple[bool, str]:
        try:
            import httpx
        except Exception:  # noqa: BLE001 - httpx is optional
            return False, "httpx is not installed; cannot reach the control plane"

        from skyn3t.config.settings import get_settings

        settings = get_settings()
        url = f"http://{settings.host}:{settings.port}/studio/approve"
        headers = {}
        token = settings.auth_token.strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "build_id": build_id,
            "approved": approve,
            "reason": "",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception:  # noqa: BLE001 - connection refused => no live process
            return False, (
                "no live build process to receive this decision "
                f"(is the control plane running at {settings.host}:{settings.port}?)"
            )
        if resp.status_code == 404:
            return False, f"build {build_id} not found on the running control plane"
        if resp.status_code // 100 != 2:
            return False, f"control plane refused the decision (HTTP {resp.status_code})"
        return True, ""

    try:
        ok, err = asyncio.run(_post())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not record decision:[/red] {exc}")
        raise typer.Exit(code=1)
    if not ok:
        console.print(f"[red]Could not record decision:[/red] {err}")
        raise typer.Exit(code=1)
    verb = "approved" if approve else "rejected"
    color = "green" if approve else "red"
    console.print(f"Build [bold]{build_id}[/bold] [{color}]{verb}[/{color}].")


@project_app.command("list")
def project_list(limit: int = typer.Option(20, "--limit", help="Max builds to show.")) -> None:
    """List recent builds from memory (empty when nothing has been built)."""
    console = _console()
    builds = asyncio.run(_recent_builds(limit))
    if not builds:
        console.print("No builds yet. Run [cyan]skyn3t studio build \"<brief>\"[/cyan].")
        return
    table = _table("Recent builds", ["build_id", "slug", "stack", "status", "verdict", "score"])
    for b in builds:
        table.add_row(
            str(b.get("build_id", ""))[:12],
            str(b.get("slug", "")),
            str(b.get("stack", "")),
            str(b.get("status", "")),
            str(b.get("verdict", "")),
            str(b.get("score", "")),
        )
    console.print(table)


@project_app.command("cleanup")
def project_cleanup(
    apply_changes: bool = typer.Option(False, "--apply", help="Actually move to trash (default: dry-run)."),
    categories: str = typer.Option("", "--categories", help="Comma list: failed,superseded,orphaned_worktrees,orphaned_projects,stray_previews."),
) -> None:
    """Report (and with --apply, trash) failed/superseded/orphaned build artifacts."""
    from skyn3t.config.settings import get_settings
    from skyn3t.studio.cleanup import apply as cleanup_apply
    from skyn3t.studio.cleanup import scan as cleanup_scan

    console = _console()
    s = get_settings()
    worktrees = s.projects_dir.parent / ".skyn3t_worktrees"
    report = cleanup_scan(s.projects_dir, worktrees)
    cats = [c.strip() for c in categories.split(",") if c.strip()]
    if not cats:
        # Safe default: failed/superseded/stray_previews require a saved manifest
        # with a terminal status, so an in-flight build (no manifest yet) is never
        # selected. orphaned_worktrees/orphaned_projects need --categories to opt in.
        cats = ["failed", "superseded", "stray_previews"]
        console.print("[dim]orphaned_worktrees/orphaned_projects are excluded by default "
                      "(they can't be told apart from an in-flight build). Opt in with "
                      "--categories orphaned_worktrees,orphaned_projects only when no build is running.[/dim]")
    items = report.all_items(cats)
    table = _table("Cleanup candidates", ["category", "path", "reason", "MB"])
    for name in ("failed", "superseded", "orphaned_worktrees", "orphaned_projects", "stray_previews"):
        if name not in cats:
            continue
        for it in getattr(report, name):
            table.add_row(name, it.path.name, it.reason, f"{it.size_bytes/1e6:.1f}")
    console.print(table)
    trash = s.projects_dir.parent / ".skyn3t_trash"
    res = cleanup_apply(report, trash_dir=trash, dry_run=not apply_changes, categories=cats)
    if res.dry_run:
        console.print(f"[yellow]dry-run[/yellow]: would free {res.freed_bytes/1e6:.1f} MB "
                      f"from {len(items)} items. Re-run with --apply to trash them.")
    else:
        console.print(f"[green]moved[/green] {len(res.moved)} items to {trash} "
                      f"({res.freed_bytes/1e6:.1f} MB).")


async def _recent_builds(limit: int) -> list[dict[str, Any]]:
    try:
        from skyn3t.config.settings import get_settings
        from skyn3t.memory.store import MemoryStore

        store = MemoryStore(get_settings())
        await store.init_db()
        return await store.recent_builds(limit=limit)
    except Exception:  # noqa: BLE001
        return []


@app.command()
def snapshot(
    out: str = typer.Option("", "--out", help="Output path (default: <data_dir>/snapshot.json)."),
) -> None:
    """Save spine state (event history snapshot) to a JSON file."""
    console = _console()
    from skyn3t.config.settings import get_settings
    from skyn3t.persistence.checkpoint import CheckpointManager

    settings = get_settings()
    # Capture *persisted* spine state, not a fresh empty in-memory bus. The
    # running spine periodically checkpoints its EventBus snapshot to disk;
    # ``latest`` is that durable state. A brand-new EventBus() would always
    # snapshot zero events.
    cp = CheckpointManager(settings).load("latest")
    if cp is None:
        console.print(
            "[yellow]No spine state available[/yellow] — no checkpoint found "
            f"under [cyan]{settings.data_dir / 'checkpoints'}[/cyan]. "
            "Run the spine first ([cyan]skyn3t start[/cyan])."
        )
        raise typer.Exit(code=1)
    snap = cp.event_bus or {}
    target = Path(out) if out else (settings.data_dir / "snapshot.json")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Failed to write snapshot:[/red] {exc}")
        raise typer.Exit(code=1)
    n_events = snap.get("published", snap.get("published_count", len(snap.get("history", []))))
    console.print(f"Snapshot written to [cyan]{target}[/cyan] ({n_events} events).")


@domain_app.command("ingest")
def domain_ingest(
    source: str = typer.Argument(..., help="Local path (file/dir) or http(s):// URL."),
) -> None:
    """Ingest a path or URL into the knowledge base (RAG corpus)."""
    console = _console()
    count = asyncio.run(_ingest_source(source))
    if count < 0:
        console.print("[red]Knowledge engine unavailable (rag package missing).[/red]")
        raise typer.Exit(code=1)
    console.print(f"Ingested [green]{count}[/green] chunks from [cyan]{source}[/cyan].")
    # Phase B/B3: a directory of markdown skills also distills advisory skills
    # (one per .md), so `domain ingest <agent-skills-dir>` grows the library.
    p = Path(source)
    if p.is_dir():
        n = _import_skills_from_dir(p)
        if n:
            console.print(
                f"Distilled [green]{n}[/green] advisory skills (one per .md) "
                f"from [cyan]{source}[/cyan]."
            )


async def _ingest_source(source: str) -> int:
    try:
        from skyn3t.config.settings import get_settings
        from skyn3t.rag.rag_engine import RagEngine
    except Exception:  # noqa: BLE001
        return -1

    settings = get_settings()
    try:
        engine = RagEngine(persist_path=settings.vector_db_path)
    except Exception:  # noqa: BLE001
        return -1

    # Learn from a GitHub repo: pull README + metadata (redacted) into RAG so it
    # informs future builds. Uses SKYN3T_GITHUB_TOKEN if configured (UI/​.env).
    if "github.com/" in source:
        text = await _fetch_github_repo(source)
        if not text:
            return 0
        try:
            return engine.ingest_text(text, source=source, kind="github")
        except Exception:  # noqa: BLE001
            return 0

    if source.startswith(("http://", "https://")):
        text = await _fetch_url(source)
        if text is None:
            return 0
        try:
            return engine.ingest_text(text, source=source, kind="web")
        except Exception:  # noqa: BLE001
            return 0

    path = Path(source)
    try:
        if path.is_dir():
            return engine.ingest_directory(str(path))
        if path.is_file():
            return engine.ingest_file(str(path))
    except Exception:  # noqa: BLE001
        return 0
    return 0


def _import_skills_from_dir(path: Path) -> int:
    """Import a directory of markdown skill files into the advisory SkillLibrary.

    Best-effort: ingestion has already succeeded; a skill-import failure must not
    fail the command. Returns the number of skills imported.
    """
    try:
        from skyn3t.config.settings import get_settings
        from skyn3t.intelligence.skill_library import SkillLibrary

        settings = get_settings()
        lib = SkillLibrary(settings.data_dir / "skills")
        return lib.import_directory(path)
    except Exception:  # noqa: BLE001
        return 0


_HTML_HINT = re.compile(r"(?is)<(!doctype html|html|head|body|div|p|span|a|article|section)\b")


def _looks_like_html(text: str) -> bool:
    return bool(_HTML_HINT.search(text or ""))


def _html_to_text(raw: str) -> str:
    """Strip HTML to readable text without a heavy dependency (no bs4).

    Drops script/style/noscript/template blocks, turns block-level closers into
    line breaks so words don't run together, removes remaining tags, unescapes
    entities, and collapses whitespace. Degrades gracefully on malformed input.
    """
    no_blocks = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1>", " ", raw or "")
    broken = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr|/section|/article)\s*/?>", "\n", no_blocks)
    no_tags = re.sub(r"(?s)<[^>]+>", " ", broken)
    text = html.unescape(no_tags)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]*\n[ \t\n]*", "\n\n", text)
    return text.strip()


async def _fetch_url(url: str) -> str | None:
    try:
        import httpx
    except Exception:  # noqa: BLE001 - httpx is optional
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text = resp.text
            ctype = resp.headers.get("content-type", "").lower()
            # Store readable text, not raw markup: a web page ingested as raw
            # HTML pollutes the RAG corpus with tags/scripts/styles.
            if "html" in ctype or (not ctype and _looks_like_html(text)):
                text = _html_to_text(text)
            return text
    except Exception:  # noqa: BLE001
        return None


async def _fetch_github_repo(url: str) -> str | None:
    """Fetch a repo's description + README (redacted) for RAG ingestion.

    Thin delegate to :func:`skyn3t.agents.github_fetch.fetch_github_repo_text`,
    the single source of truth shared with the Cortex INGEST handler.
    """
    from skyn3t.agents.github_fetch import fetch_github_repo_text

    return await fetch_github_repo_text(url)


# ---------------------------------------------------------------------------
# doctor sub-checks (module level, import-light)
# ---------------------------------------------------------------------------
def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def _check_db(settings: Any) -> tuple[str, bool]:
    async def _try() -> bool:
        from skyn3t.memory.store import MemoryStore

        store = MemoryStore(settings)
        await store.init_db()
        return True

    try:
        ok = asyncio.run(_try())
        return (settings.db_url, ok)
    except Exception as exc:  # noqa: BLE001
        return (f"init failed: {exc}", False)


def _check_llm(settings: Any) -> tuple[str, bool]:
    try:
        from skyn3t.adapters.llm import LLMClient

        client = LLMClient(settings)
        backend = getattr(client, "backend", "stub")
        return (backend, backend != "stub")
    except Exception as exc:  # noqa: BLE001
        return (f"unavailable: {exc}", False)


def _check_sandbox(settings: Any) -> str:
    backend = getattr(settings, "execution_backend", "auto")
    docker_present = _has_module("docker")
    if backend == "docker" and not docker_present:
        return "docker requested but SDK absent -> falls back to inline"
    if backend == "auto":
        return f"auto (docker SDK {'present' if docker_present else 'absent'} -> inline fallback)"
    return backend


def _check_writable(path: Any) -> tuple[bool, str]:
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".skyn3t_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return (True, str(p))
    except Exception as exc:  # noqa: BLE001
        return (False, f"{p} ({exc})")


def main() -> None:  # pragma: no cover - console-script shim
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
