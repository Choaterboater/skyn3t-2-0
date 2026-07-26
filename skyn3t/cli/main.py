"""SkyN3t 2.0 command-line interface.

A Typer app that drives the autonomous app factory from the terminal:

  * ``skyn3t start``           boot the spine, register every available agent,
                              optionally launch the web control plane
  * ``skyn3t doctor``         readiness report (python, deps, db, llm, sandbox,
                              proof toolchain, projects dir) as a rich table
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
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Protocol
from typing import cast as type_cast

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
bench_app = typer.Typer(help="Benchmark/regression harness (Spec 2).", no_args_is_help=True)
golden_bench_app = typer.Typer(
    help="Validate and run the repeatable golden app suite.",
    no_args_is_help=True,
)
cortex_app = typer.Typer(help="Inspect the autonomy layer (cortex).", no_args_is_help=True)
audit_app = typer.Typer(help="Audit SkyN3t itself as an app factory.", no_args_is_help=True)
app.add_typer(studio_app, name="studio")
app.add_typer(project_app, name="project")
app.add_typer(domain_app, name="domain")
app.add_typer(bench_app, name="bench")
bench_app.add_typer(golden_bench_app, name="golden")
app.add_typer(cortex_app, name="cortex")
app.add_typer(audit_app, name="audit")


class _RagWithIngestor(Protocol):
    """RAG runtime augmented with its event-ingestion lifecycle owner."""

    _skyn3t_ingestor: object


# ---------------------------------------------------------------------------
# Console helpers — fall back to plain ``print`` when ``rich`` is absent.
# ---------------------------------------------------------------------------
def _console() -> Any:
    # Windows terminals and redirected streams can still expose a legacy
    # encoding such as CP1252. Rich ultimately writes to that TextIOWrapper;
    # make unsupported glyphs degrade visibly instead of crashing the command.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (OSError, ValueError):
                pass
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
    ("skyn3t.agents.stack_detector", "StackDetectorAgent"),
    ("skyn3t.agents.env_scanner", "EnvScannerAgent"),
    ("skyn3t.agents.test_author", "TestAuthorAgent"),
    ("skyn3t.agents.config_ui_agent", "ConfigUIAgent"),
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
async def _assemble_spine(
    *,
    with_memory: bool = True,
    event_bus: Any | None = None,
    settings_override: Any | None = None,
) -> dict[str, Any]:
    """Wire event bus, orchestrator, llm, router, memory, and agents.

    Returns a dict of collaborators. Every piece degrades independently.
    Pass ``event_bus`` to share one bus with the web layer's WebSocket bridge.
    """
    from skyn3t.config.settings import get_settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator

    settings = settings_override or get_settings()
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
            memory.attach_event_bus(event_bus)
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

    # Replay approved Cortex prompt overrides before any CLI build starts. The
    # previous replay lived in build_cortex(), but studio build constructs Cortex
    # only after runner.start(), so learned instructions missed the active build.
    try:
        from skyn3t.cortex.prompt_store import load_prompt_overrides

        overrides = load_prompt_overrides(settings.data_dir)
        if overrides:
            from skyn3t.cortex.handlers import HandlerRegistry

            handlers = HandlerRegistry(
                settings=settings,
                agents=orchestrator.agents,
                data_dir=settings.data_dir,
            )
            for target, instruction in overrides.items():
                handlers._apply_prompt_to_live(target, instruction)
    except Exception:  # noqa: BLE001 - prompt replay must never block startup
        pass

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
            raise typer.Exit(code=1) from exc
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
def doctor(
    stack: Annotated[
        str,
        typer.Option(
            "--stack",
            help=(
                "Generated-app stack whose proof tools should be evaluated "
                "(for example react or react_native)."
            ),
        ),
    ] = "",
) -> None:
    """Print readiness for the runtime and stack-appropriate proof toolchain."""
    console = _console()
    import platform
    import sys

    from skyn3t.config.settings import get_settings
    from skyn3t.observability.health import (
        Status,
        lab_tool_check_result,
        lab_tool_unknown_result,
    )
    from skyn3t.studio.lab_tools import inspect_lab_toolchain

    settings = get_settings()
    normalized_stack = str(stack or "").strip().lower()
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

    # Blocking external proof tools. Readiness means the command actually runs;
    # Docker in particular checks the daemon rather than only the CLI binary.
    proof_ladder_required = bool(
        getattr(settings, "proof_ladder_required", True)
    )

    def status_marker(result: Any, ready: bool | None) -> str:
        if ready is True:
            return _ok(True)
        if ready is None:
            if result.status is Status.FAIL:
                return "[red]ERROR[/red]"
            if result.status is Status.DEGRADED:
                return "[yellow]UNKNOWN[/yellow]"
            return "[dim]N/A[/dim]"
        if result.status is Status.FAIL:
            return "[red]MISSING[/red]"
        if result.status is Status.DEGRADED:
            return "[yellow]optional[/yellow]"
        return "[dim]N/A[/dim]"

    lab_rows: list[tuple[str, str, str]] = []
    try:
        lab_report = inspect_lab_toolchain(stack=normalized_stack)
        checks = getattr(lab_report, "checks", None)
        if not isinstance(checks, dict):
            raise TypeError("toolchain report checks must be a mapping")
        for tool_name in ("docker", "playwright", "maestro"):
            check = checks.get(tool_name)
            if check is None:
                result = lab_tool_unknown_result(
                    tool_name,
                    proof_ladder_required=proof_ladder_required,
                    stack=normalized_stack,
                    reason=f"lab toolchain report omitted {tool_name}",
                )
                lab_rows.append(
                    (tool_name, status_marker(result, None), result.detail)
                )
                continue
            try:
                result = lab_tool_check_result(
                    check,
                    proof_ladder_required=proof_ladder_required,
                    stack=normalized_stack,
                )
                ready: bool | None = bool(check.ready)
            except Exception as exc:  # noqa: BLE001 - malformed check is unknown
                result = lab_tool_unknown_result(
                    tool_name,
                    proof_ladder_required=proof_ladder_required,
                    stack=normalized_stack,
                    reason=f"malformed tool check: {exc}",
                )
                ready = None
            lab_rows.append(
                (tool_name, status_marker(result, ready), result.detail)
            )
    except Exception as exc:  # noqa: BLE001 - doctor reports and remains exit-zero
        reason = f"toolchain inspection failed: {exc}"[:500]
        for tool_name in ("docker", "playwright", "maestro"):
            result = lab_tool_unknown_result(
                tool_name,
                proof_ladder_required=proof_ladder_required,
                stack=normalized_stack,
                reason=reason,
            )
            lab_rows.append(
                (tool_name, status_marker(result, None), result.detail)
            )
    for tool_name, status, detail in lab_rows:
        table.add_row(f"lab:{tool_name}", status, detail)

    # Projects dir writable.
    proj_ok, proj_detail = _check_writable(settings.projects_dir)
    table.add_row("projects dir", _ok(proj_ok), proj_detail)

    console.print(table)
    console.print(
        f"policy: free_only={settings.free_only} no_claude={settings.no_claude} "
        f"approval_gates={settings.approval_gates} "
        f"provider_key_configured={settings.has_any_llm}"
    )


@studio_app.command("build")
def studio_build(
    brief: str = typer.Argument(..., help="What to build, in plain English."),
    best_of: int = typer.Option(0, "--best-of", "-n", help="Best-of-N code trajectories."),
    no_critic: bool = typer.Option(False, "--no-critic", help="Disable the adversarial critic gate."),
    slug: str = typer.Option("", "--slug", help="Override the project slug."),
    stack: str = typer.Option("", "--stack", help="Pin the stack: react|nextjs|fastapi|static|python|express|phaser|…"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the plan confirmation and build immediately."),
) -> None:
    """Show the plan (what + which stack), confirm, then run the build end to end.

    The plan preview catches a wrong guess — e.g. a python CLI when you meant a web
    app — BEFORE a full build. Auto-confirmed with --yes or when non-interactive (CI).
    """
    console = _console()
    import sys as _sys

    def _confirm_plan(plan: dict) -> bool:
        conf = float(plan.get("confidence") or 0.0)
        low = conf < 0.6 or plan.get("ambiguous")
        console.print(
            f"\n[bold]Plan[/bold] — a [cyan]{plan.get('app_type', 'app')}[/cyan] built as "
            f"[bold]{plan.get('stack')}[/bold] ([dim]{plan.get('engine')}[/dim]), "
            f"ships as [dim]{plan.get('deploy_kind', '—')}[/dim].")
        tone = "yellow" if low else "green"
        console.print(f"  confidence [{tone}]{conf:.0%}[/{tone}] — {plan.get('rationale', '')}")
        qs = plan.get("questions") or []
        if qs:
            console.print("  [yellow]I had to assume:[/yellow] " + " · ".join(qs[:3]))
        if low:
            console.print("  [yellow]⚠ not fully sure of the shape[/yellow] — if this is wrong, "
                          "answer no and re-run with [cyan]--stack <react|nextjs|fastapi|python|…>[/cyan].")
        return typer.confirm("Build this?", default=True)

    interactive = _sys.stdin.isatty() and not yes
    outcome = asyncio.run(_run_build(
        brief, best_of=best_of, no_critic=no_critic, slug=slug, stack=stack,
        confirm=(_confirm_plan if interactive else None)))
    if outcome is None:
        console.print("[red]Build pipeline unavailable (studio package missing).[/red]")
        raise typer.Exit(code=1)
    if outcome.get("aborted"):
        console.print("[dim]Aborted — nothing built. Refine the brief or pass --stack.[/dim]")
        raise typer.Exit(code=0)

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
    fo = outcome.get("fanout")
    if isinstance(fo, dict):
        console.print(
            f"[cyan]Fan-out[/cyan]: winner [bold]{fo.get('winner')}[/bold] of "
            f"{fo.get('n')} stacks · {fo.get('passed')} passed · "
            f"exploration delta +{fo.get('delta')} · "
            f"trashed {len(fo.get('trashed_losers', []))} loser(s)")
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
        enabled=bool(
            getattr(settings, "debate_enabled", False)
            or getattr(settings, "a2a_conversation", False)
        ),
        tournament=tournament,
        event_bus=EventBus(),
    )


@app.command()
def debate(
    question: str = typer.Argument(..., help="The question to debate across models."),
) -> None:
    """Multi-model debate: propose -> cross-examine -> vote -> synthesise.

    Gated by SKYN3T_DEBATE_ENABLED or SKYN3T_A2A_CONVERSATION: off (default)
    is a single cheap completion; on runs several models and records the winner
    into the ModelTournament that feeds the learned router. Degrades
    deterministically on the stub backend.
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


@cortex_app.command("status")
def cortex_status() -> None:
    """Show what cortex has actually changed: the learned-router leaderboard
    (fed by real builds), persisted tuning overrides, and prompt overrides.

    All three are read from ``data/`` — proof the self-improvement loops take
    effect, not just emit proposals. Offline and side-effect-free.
    """
    from skyn3t.config.settings import get_settings

    console = _console()
    settings = get_settings()
    data_dir = settings.data_dir

    # 1) Learned-router leaderboard (ModelTournament fed per successful stage).
    try:
        from skyn3t.intelligence.model_tournament import ModelTournament

        snap = ModelTournament(data_dir / "model_tournament.json").snapshot()
    except Exception:  # noqa: BLE001
        snap = {}
    lb = _table("Learned router — model leaderboard", ["bucket", "model", "rating", "win%", "plays"])
    rows = 0
    for bucket, entries in (snap or {}).items():
        for e in entries:
            lb.add_row(bucket, str(e["model"]), str(e["rating"]),
                       f"{e['win_rate'] * 100:.0f}", str(e["plays"]))
            rows += 1
    console.print(lb if rows else "[dim]No tournament data yet — run some builds.[/dim]")

    # 2) Persisted tuning overrides (settings that cortex tuned and that carry
    #    across builds).
    try:
        from skyn3t.cortex.tuning_store import load_overrides

        tuning = load_overrides(data_dir)
    except Exception:  # noqa: BLE001
        tuning = {}
    tt = _table("Applied tuning overrides", ["setting", "value"])
    for k, v in (tuning or {}).items():
        tt.add_row(str(k), str(v))
    console.print(tt if tuning else "[dim]No tuning overrides applied.[/dim]")

    # 3) Persisted prompt overrides (evolved agent instructions, by capability).
    try:
        from skyn3t.cortex.prompt_store import load_prompt_overrides

        prompts = load_prompt_overrides(data_dir)
    except Exception:  # noqa: BLE001
        prompts = {}
    pt = _table("Evolved agent instructions", ["agent", "instruction"])
    for agent, instr in (prompts or {}).items():
        text = instr if len(instr) <= 80 else instr[:77] + "..."
        pt.add_row(str(agent), text)
    console.print(pt if prompts else "[dim]No prompt overrides applied.[/dim]")


def _ratchet_coerce(v: str) -> Any:
    low = v.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            continue
    return v


def _make_ratchet_build_fn():
    """A bench build_fn that LAZILY assembles a fresh spine + StudioRunner on first
    use — so a run after a persisted override (with the get_settings cache cleared)
    picks it up. Each factory call returns a new closure = a fresh runner."""
    holder: dict[str, Any] = {}

    async def build_fn(case):
        if "runner" not in holder:
            from skyn3t.studio.runner import StudioRunner

            spine = await _assemble_spine()
            settings = spine["settings"]
            learning, patterns, skills, rag = _build_intelligence(
                settings, spine["event_bus"], spine["memory"])
            cost_tracker, budget_guard = _build_observability(settings, spine["llm"])
            holder["runner"] = StudioRunner(
                spine["event_bus"], spine["orchestrator"], settings=settings,
                memory=spine["memory"], learning=learning, patterns=patterns,
                skills=skills, cost_tracker=cost_tracker, budget_guard=budget_guard, rag=rag)
            holder["llm"] = spine.get("llm")
        _reset_bench_budget(holder["llm"])
        extra = {"stack": case.stack} if case.stack else {}
        return await holder["runner"].start(case.brief, slug=None, extra=extra)

    return build_fn

_RATCHET_SET_OPTION = typer.Option(
    None, "--set", help="A tuning override to test, e.g. --set best_of_n=3 (repeatable)."
)


@cortex_app.command("ratchet")
def cortex_ratchet(
    set_kv: list[str] | None = _RATCHET_SET_OPTION,
    cases: str = typer.Option("", "--cases", help="Path to a JSON bench-case list (default: the built-in exam)."),
    suite: str = typer.Option("apps", "--suite", help="Built-in suite when --cases is omitted: apps|all|games."),
    min_score_delta: float = typer.Option(0.0, "--min-score-delta", help="Required go-only mean-score improvement to keep."),
) -> None:
    """Keep a proposed tuning change ONLY if a bench run measurably raises the
    go-rate — no aggregate AND no per-app-type regression — else revert it.

    Opt-in (Settings.reliability_ratchet_enabled). WARNING: runs the bench TWICE
    (before + after) = real builds, so it costs time + money.
    """
    console = _console()
    from skyn3t.config.settings import get_settings
    from skyn3t.cortex.ratchet import evaluate_change, restore_overrides, snapshot_overrides
    from skyn3t.cortex.tuning_store import PERSISTABLE_TUNING, persist_overrides
    from skyn3t.studio.bench import built_in_cases

    s = get_settings()
    if not getattr(s, "reliability_ratchet_enabled", False):
        console.print("[yellow]Ratchet is off.[/yellow] Enable "
                      "[cyan]reliability_ratchet_enabled[/cyan] in Settings to run it.")
        raise typer.Exit(code=1)

    overrides: dict[str, Any] = {}
    for kv in set_kv or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            overrides[k.strip()] = _ratchet_coerce(v)
    if not overrides:
        console.print("[red]Nothing to test[/red] — pass at least one [cyan]--set key=value[/cyan].")
        raise typer.Exit(code=1)
    bad = [k for k in overrides if k not in PERSISTABLE_TUNING]
    if bad:
        console.print(f"[red]Not a persistable tuning key[/red]: {', '.join(bad)}. "
                      f"Allowed: {', '.join(sorted(PERSISTABLE_TUNING))}")
        raise typer.Exit(code=1)

    data_dir = s.data_dir
    snap = snapshot_overrides(data_dir)

    def apply():
        persist_overrides(data_dir, overrides)
        get_settings.cache_clear()

    def revert():
        restore_overrides(data_dir, snap)
        get_settings.cache_clear()

    bench_cases = _load_bench_cases(cases) or built_in_cases(suite)
    console.print(f"[yellow]Ratchet[/yellow] testing {overrides} across "
                  f"{len(bench_cases)} cases (before + after — real builds)…")
    res = asyncio.run(evaluate_change(
        apply_change=apply, revert_change=revert, make_build_fn=_make_ratchet_build_fn,
        cases=bench_cases, label="ratchet",
        gate_kwargs={"min_mean_score_delta": min_score_delta},
    ))
    b, a = res.get("before", {}), res.get("after", {})
    console.print(f"go-rate [bold]{b.get('go_rate')}[/bold] → [bold]{a.get('go_rate')}[/bold] "
                  f"(Δ {res.get('go_rate_delta')})")
    if res.get("kept"):
        console.print("[green]KEPT[/green] — measurably better; the override is persisted.")
    else:
        console.print("[yellow]REVERTED[/yellow] — "
                      + "; ".join(res.get("reasons") or ["no improvement"]))


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

        ingestor = ExperienceIngestor(event_bus, rag_engine=rag)
        ingestor.start()
        managed_rag = type_cast(_RagWithIngestor, rag)
        managed_rag._skyn3t_ingestor = ingestor
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


def _trash_fanout_losers(settings: Any, base: str, cands: list, winner_id: str | None) -> list[str]:
    """Trash the non-winner fan-out candidate projects (recoverable). Returns the
    candidate ids that were trashed."""
    from skyn3t.studio.cleanup import trash_path
    trash_dir = settings.projects_dir.parent / ".skyn3t_trash"
    trashed: list[str] = []
    for c in cands:
        if c.id == winner_id:
            continue
        loser = settings.projects_dir / f"{base}-{c.id}"
        if loser.is_dir():
            try:
                trash_path(loser, trash_dir)
                trashed.append(c.id)
            except OSError:
                pass
    return trashed


async def _plan_preview(brief: str, stack: str, llm: Any) -> dict[str, Any]:
    """Classify a brief WITHOUT building — the same stack the build will use, its
    confidence + rationale, the app-type/engine, how it ships, and any clarifying
    questions. This is the 'here's what I'll build — confirm?' data, so a wrong
    guess is caught in one glance instead of after a full build."""
    from skyn3t.studio.clarification import analyze
    from skyn3t.studio.stack_selector import classify_build, select_stack

    choice = await select_stack(brief, pin=stack, llm=llm, attended=True)
    cls = classify_build(brief, choice.stack)
    clar = analyze(brief)
    try:
        from skyn3t.studio.deploy import DEPLOY_KIND
        deploy_kind = DEPLOY_KIND.get(choice.stack, "—")
    except Exception:  # noqa: BLE001
        deploy_kind = "—"
    return {
        "stack": choice.stack, "method": choice.method,
        "confidence": choice.confidence, "rationale": choice.rationale,
        "app_type": cls.app_type, "engine": cls.engine, "deploy_kind": deploy_kind,
        "questions": [q.question for q in getattr(clar, "questions", [])],
        "ambiguous": bool(getattr(clar, "ambiguous", False)),
    }


async def _run_build(brief: str, *, best_of: int, no_critic: bool, slug: str, stack: str = "",
                     confirm: Any = None) -> dict[str, Any] | None:
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
    # Spec 4: autonomous fan-out. An UNPINNED build, when settings configure a
    # stack list, is explored across those stacks in parallel and the proof
    # WINNER is delivered. Off by default (empty setting) -> normal single build.
    from skyn3t.studio.fanout import FanCandidate, autonomous_stacks, fan_out
    fo_stacks = autonomous_stacks(settings, has_pin=bool(stack))
    if fo_stacks:
        from skyn3t.studio.runner import _slugify
        base = _slugify(slug or brief)
        cands = [FanCandidate(id=s, label=s, spec={"stack": s}) for s in fo_stacks]
        raw: dict[str, Any] = {}

        async def _fan_build(c):
            out = await runner.start(brief, slug=f"{base}-{c.id}",
                                     extra={"stack": (c.spec or {}).get("stack", "")})
            raw[c.id] = out
            return out

        fo = await fan_out(cands, _fan_build, event_bus=spine["event_bus"])
        winner_id = fo.winner.candidate_id if fo.winner is not None else None
        # An autonomous build delivers ONE winner; trash the loser candidate
        # projects (recoverable in .skyn3t_trash) so they don't clutter Projects/.
        trashed = _trash_fanout_losers(settings, base, cands, winner_id)
        summary = {**fo.summary, "trashed_losers": trashed}
        if winner_id is not None and winner_id in raw:
            d = raw[winner_id].to_dict()
            d["fanout"] = summary
            return d
        return {"fanout": summary, "winner": None}

    extra: dict[str, Any] = {}
    # ``--best-of 1`` is an explicit request to suppress the configured
    # best-of-two default for a controlled, single-trajectory build. Preserve
    # every positive CLI value instead of treating one as if it were omitted.
    if best_of >= 1:
        extra["best_of_n"] = best_of
    if stack:
        extra["stack"] = stack
    # Plan preview + confirm: classify BEFORE building so a wrong guess (e.g. a
    # python CLI when you meant a web app) is caught in one glance, not after a
    # full build. Skipped when confirm is None (unattended / --yes / non-TTY). The
    # confirmed stack is pinned so the build matches exactly what was previewed.
    if confirm is not None:
        plan = await _plan_preview(brief, stack, spine.get("llm"))
        if not confirm(plan):
            return {"aborted": True, "stack": plan.get("stack", "")}
        extra["stack"] = plan["stack"]
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


# ---------------------------------------------------------------------------
# Benchmark / regression harness (Spec 2)
# ---------------------------------------------------------------------------

async def _bench_run_async(cases, label):
    """Assemble the spine + a StudioRunner once, then build every case."""
    import time as _time

    from skyn3t.studio.bench import run_bench
    from skyn3t.studio.runner import StudioRunner

    spine = await _assemble_spine()
    settings = spine["settings"]
    learning, patterns, skills, rag = _build_intelligence(
        settings, spine["event_bus"], spine["memory"])
    cost_tracker, budget_guard = _build_observability(settings, spine["llm"])
    runner = StudioRunner(
        spine["event_bus"], spine["orchestrator"], settings=settings,
        memory=spine["memory"], learning=learning, patterns=patterns, skills=skills,
        cost_tracker=cost_tracker, budget_guard=budget_guard, rag=rag,
    )

    llm = spine.get("llm")

    async def build_fn(case):
        # Reset the shared LLM's cumulative spend so a long sweep doesn't starve
        # trailing cases (the daily/token caps would otherwise make the bench
        # order-dependent). Per-case isolation is the whole point of the harness.
        _reset_bench_budget(llm)
        extra = {"stack": case.stack} if case.stack else {}
        return await runner.start(case.brief, slug=None, extra=extra)

    run = await run_bench(cases, build_fn, label=label, created_at=_time.time())
    return run, settings


def _reset_bench_budget(llm) -> None:
    budget = getattr(llm, "budget", None)
    if budget is None:
        return
    if hasattr(budget, "reset_build"):
        try:
            budget.reset_build()
        except Exception:  # noqa: BLE001
            pass
    for attr, zero in (("spent_build", 0.0),):
        if hasattr(budget, attr):
            try:
                setattr(budget, attr, zero)
            except Exception:  # noqa: BLE001
                pass


def _load_bench_cases(path: str):
    import json as _json
    from pathlib import Path as _Path

    from skyn3t.studio.bench import BenchCase
    if not path:
        return None
    raw_text = str(path).strip()
    try:
        if raw_text.startswith("[") or raw_text.startswith("{"):
            raw = _json.loads(raw_text)
        else:
            raw = _json.loads(_Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    out = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, dict) and r.get("brief"):
            out.append(BenchCase(id=str(r.get("id") or r["brief"][:24]),
                                 brief=str(r["brief"]), stack=str(r.get("stack", ""))))
    return out or None


def _print_bench_summary(console, run) -> None:
    s = run.summary
    table = _table(f"Bench '{run.label}'", ["case", "stack", "verdict", "score", "intent", "cost $"])
    for r in run.results:
        table.add_row(
            r.case_id, r.stack or "—", r.verdict,
            "—" if r.score is None else f"{r.score:.1f}",
            "—" if r.intent_score is None else f"{r.intent_score:.1f}",
            "—" if r.cost_usd is None else f"{r.cost_usd:.4f}")
    console.print(table)
    cpg = s.get("cost_per_go_usd")
    console.print(
        f"go-rate [bold]{s.get('go_rate', 0) * 100:.0f}%[/bold] "
        f"({s.get('go', 0)}/{s.get('n', 0)}) · mean score {s.get('mean_score')} "
        f"· mean intent {s.get('mean_intent')} · total ${s.get('total_cost_usd')} "
        f"· $/go {'—' if cpg is None else f'{cpg:.4f}'}")


async def _fanout_async(brief: str, cands):
    """Assemble the spine + a StudioRunner once, then build every candidate
    (divergent stacks) in parallel for the same brief."""
    from skyn3t.studio.fanout import fan_out
    from skyn3t.studio.runner import StudioRunner, _slugify

    spine = await _assemble_spine()
    settings = spine["settings"]
    learning, patterns, skills, rag = _build_intelligence(
        settings, spine["event_bus"], spine["memory"])
    cost_tracker, budget_guard = _build_observability(settings, spine["llm"])
    runner = StudioRunner(
        spine["event_bus"], spine["orchestrator"], settings=settings,
        memory=spine["memory"], learning=learning, patterns=patterns, skills=skills,
        cost_tracker=cost_tracker, budget_guard=budget_guard, rag=rag,
    )
    base = _slugify(brief)

    async def build_fn(c):
        # NOTE: no per-candidate budget reset here. fan-out builds run CONCURRENTLY
        # (asyncio.gather), so resetting the shared budget mid-flight would race
        # and let a candidate escape the daily cap. A fan-out is ONE exploration —
        # spend should accumulate so the cap protects the whole sweep. (The runner
        # still resets the per-BUILD counter itself via cost_tracker.start_build.)
        stack = (c.spec or {}).get("stack", "")
        # distinct slug per candidate so they don't clobber each other's project
        return await runner.start(brief, slug=f"{base}-{c.id}",
                                  extra={"stack": stack} if stack else {})

    return await fan_out(cands, build_fn)


@app.command("fanout")
def fanout_cmd(
    brief: str = typer.Argument(..., help="The brief to explore across stacks."),
    stacks: str = typer.Option("react,static,fastapi",
                                "--stacks", "-s", help="Comma-separated candidate stacks."),
) -> None:
    """Spec 4: build N divergent stack candidates for one brief, referee by
    proof, and report the winner + exploration delta."""
    from skyn3t.studio.fanout import FanCandidate
    console = _console()
    ids = [s.strip() for s in stacks.split(",") if s.strip()]
    if len(ids) < 2:
        console.print("[yellow]Give at least two --stacks to fan out.[/yellow]")
        raise typer.Exit(code=1)
    cands = [FanCandidate(id=s, label=s, spec={"stack": s}) for s in ids]
    console.print(f"[cyan]Fan-out[/cyan] — building {len(cands)} divergent candidates "
                  "(real builds, this can take a while).")
    out = asyncio.run(_fanout_async(brief, cands))
    table = _table("Fan-out candidates", ["candidate", "verdict", "score", "proof", "status"])
    for r in out.results:
        table.add_row(r.candidate_id, r.verdict,
                      "—" if r.score is None else f"{r.score:.1f}",
                      "pass" if r.proof_passed else "fail", r.status)
    console.print(table)
    if out.winner is not None:
        tone = "green" if out.any_passed else "yellow"
        console.print(f"[{tone}]Winner:[/{tone}] [bold]{out.winner.candidate_id}[/bold] "
                      f"(score {out.winner.score}) · exploration delta +{out.delta} "
                      f"· {'a candidate passed' if out.any_passed else 'none passed — most complete'}")
    else:
        console.print("[red]No candidates produced a result.[/red]")


async def _golden_run_async(
    suite,
    *,
    out_path: Path,
    report_path: Path,
    repeats: int,
    seed: int,
    execution_backend: str,
    llm_backend: str,
    work_root: Path | None,
):
    """Execute golden cases through fresh, isolated ``StudioRunner.start`` paths."""
    from skyn3t.adapters.llm import BudgetTracker
    from skyn3t.config.settings import get_settings
    from skyn3t.studio.golden_bench import (
        benchmark_settings_profile,
        isolated_settings,
        run_golden,
    )
    from skyn3t.studio.runner import StudioRunner

    base_settings = get_settings()
    settings_profile = benchmark_settings_profile(base_settings, llm_backend=llm_backend)
    shared_budget = None

    async def build_fn(case, context):
        nonlocal shared_budget
        settings = isolated_settings(
            base_settings,
            context.workspace_dir,
            llm_backend=llm_backend,
            execution_backend=execution_backend,
        )
        spine = await _assemble_spine(settings_override=settings)
        rag = None
        try:
            # State and projects are isolated per attempt, but daily spend is a
            # process-wide safety boundary. Reuse the host ledger across every
            # fresh LLM client while CostTracker resets only the per-build counter.
            llm = spine.get("llm")
            if llm is not None:
                if shared_budget is None:
                    shared_budget = BudgetTracker(
                        per_build_cap=base_settings.per_build_usd_cap,
                        daily_cap=base_settings.daily_usd_cap,
                        token_cap=base_settings.daily_token_cap,
                        ledger_path=base_settings.data_dir / "budget" / "daily_usage.json",
                    )
                llm.budget = shared_budget
                _reset_bench_budget(llm)
            learning, patterns, skills, rag = _build_intelligence(
                settings, spine["event_bus"], spine["memory"]
            )
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
            return await runner.start(
                case.brief,
                slug=context.slug,
                extra={
                    "stack": case.stack,
                    "build_id": f"golden-{context.seed:016x}",
                    "best_of_n": 1,
                    "parallel_code_slices": False,
                    "attended": False,
                },
            )
        finally:
            ingestor = getattr(rag, "_skyn3t_ingestor", None)
            stop = getattr(ingestor, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:  # noqa: BLE001 - cleanup must not hide build evidence
                    pass
            close = getattr(spine.get("memory"), "close", None)
            if callable(close):
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:  # noqa: BLE001 - cleanup must not hide build evidence
                    pass

    return await run_golden(
        suite,
        build_fn,
        out_path=out_path,
        report_path=report_path,
        repeats=repeats,
        seed=seed,
        llm_backend=llm_backend,
        execution_backend=execution_backend,
        work_root=work_root,
        safety_profile=settings_profile,
    )


@golden_bench_app.command("validate")
def golden_validate(
    suite_path: str = typer.Option(
        "",
        "--suite",
        help="Golden suite JSON (default: packaged golden-v1.json).",
    ),
) -> None:
    """Strictly validate the suite schema, paths, gate policy, and digest."""
    from skyn3t.studio.golden_bench import GoldenBenchError, load_suite, suite_digest

    console = _console()
    try:
        suite = load_suite(suite_path or None)
    except GoldenBenchError as exc:
        console.print(f"[red]Invalid golden suite:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    stacks = len({case.stack for case in suite.cases})
    console.print(
        f"[green]Valid[/green] [bold]{suite.suite_id}[/bold]: "
        f"{len(suite.cases)} cases across {stacks} stacks"
    )
    console.print(f"SHA-256: [cyan]{suite_digest(suite)}[/cyan]")


@golden_bench_app.command("run")
def golden_run(
    suite_path: str = typer.Option(
        "",
        "--suite",
        help="Golden suite JSON (default: packaged golden-v1.json).",
    ),
    out: str = typer.Option(
        "artifacts/golden/run.json",
        "--out",
        help="Atomic JSON ledger output.",
    ),
    report: str = typer.Option(
        "artifacts/golden/run.md",
        "--report",
        help="Atomic Markdown report output.",
    ),
    repeats: int = typer.Option(
        2,
        "--repeats",
        min=1,
        max=10,
        help="Independent repetitions per case (1-10).",
    ),
    seed: int = typer.Option(
        20260709,
        "--seed",
        min=0,
        max=2**63 - 1,
        help="Deterministic benchmark seed.",
    ),
    execution_backend: str = typer.Option(
        "inline",
        "--execution-backend",
        help="Execution backend: inline, docker, or auto.",
    ),
    llm_backend: str = typer.Option(
        "stub",
        "--llm-backend",
        help="LLM backend; stub is the safe deterministic default.",
    ),
    work_root: str = typer.Option(
        "",
        "--work-root",
        help="Optional root for isolated per-attempt state and projects.",
    ),
) -> None:
    """Run every golden contract through StudioRunner and write durable evidence."""
    from skyn3t.studio.golden_bench import GoldenBenchError, load_suite

    console = _console()
    out_path = Path(out).expanduser()
    report_path = Path(report).expanduser()
    try:
        suite = load_suite(suite_path or None)
        console.print(
            f"[cyan]Golden benchmark[/cyan] {suite.suite_id}: "
            f"{len(suite.cases)} cases x {repeats} repeats through StudioRunner"
        )
        ledger = asyncio.run(
            _golden_run_async(
                suite,
                out_path=out_path,
                report_path=report_path,
                repeats=repeats,
                seed=seed,
                execution_backend=execution_backend.strip().lower(),
                llm_backend=llm_backend.strip().lower(),
                work_root=Path(work_root).expanduser() if work_root.strip() else None,
            )
        )
    except (GoldenBenchError, OSError) as exc:
        console.print(f"[red]Golden benchmark error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt as exc:  # pragma: no cover - manual interruption
        console.print(f"[yellow]Interrupted.[/yellow] Partial ledger: {out_path}")
        raise typer.Exit(code=130) from exc

    overall = ledger.summary.overall
    console.print(
        f"[{'green' if overall.failed == 0 else 'red'}]"
        f"{overall.passed}/{overall.attempts} passed"
        f"[/{'green' if overall.failed == 0 else 'red'}] "
        f"(Wilson 95% {overall.wilson.low * 100:.1f}-{overall.wilson.high * 100:.1f}%)"
    )
    console.print(f"Ledger: [cyan]{out_path}[/cyan]")
    console.print(f"Report: [cyan]{report_path}[/cyan]")
    if ledger.status != "completed":
        raise typer.Exit(code=2)
    if overall.failed:
        raise typer.Exit(code=1)


@golden_bench_app.command("compare")
def golden_compare(
    baseline: str = typer.Option(..., "--baseline", help="Completed baseline ledger JSON."),
    candidate: str = typer.Option(..., "--candidate", help="Completed candidate ledger JSON."),
    out: str = typer.Option(
        "artifacts/golden/comparison.json",
        "--out",
        help="Atomic comparison JSON output.",
    ),
    report: str = typer.Option(
        "artifacts/golden/comparison.md",
        "--report",
        help="Atomic comparison Markdown output.",
    ),
    max_suite_pass_rate_drop: float = typer.Option(
        0.0,
        "--max-suite-pass-rate-drop",
        min=0.0,
        max=1.0,
        help="Maximum tolerated aggregate pass-rate drop (0-1).",
    ),
    min_case_pass_rate: float = typer.Option(
        1.0,
        "--min-case-pass-rate",
        min=0.0,
        max=1.0,
        help="Minimum candidate pass rate required for every case (0-1).",
    ),
) -> None:
    """Compare compatible completed ledgers and exit nonzero on regression."""
    from skyn3t.studio.golden_bench import compare_ledger_files

    console = _console()
    out_path = Path(out).expanduser()
    report_path = Path(report).expanduser()
    try:
        comparison = compare_ledger_files(
            Path(baseline).expanduser(),
            Path(candidate).expanduser(),
            out_path=out_path,
            report_path=report_path,
            max_suite_pass_rate_drop=max_suite_pass_rate_drop,
            min_case_pass_rate=min_case_pass_rate,
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]Golden comparison error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    tone = "green" if comparison.status == "passed" else "red"
    console.print(f"[{tone}]Golden comparison: {comparison.status}[/{tone}]")
    for reason in comparison.reasons:
        console.print(f"- {reason}")
    console.print(f"Comparison: [cyan]{out_path}[/cyan]")
    console.print(f"Report: [cyan]{report_path}[/cyan]")
    if comparison.status == "error":
        raise typer.Exit(code=2)
    if comparison.status != "passed":
        raise typer.Exit(code=1)


@bench_app.command("run")
def bench_run(
    label: str = typer.Option("", "--label", "-l", help="Run label (default: timestamp)."),
    cases_file: str = typer.Option("", "--cases", help="JSON [{id,brief,stack}] (default: built-in set)."),
    suite: str = typer.Option("apps", "--suite", help="Built-in suite when --cases is omitted: apps|all|games."),
    no_save: bool = typer.Option(False, "--no-save", help="Don't write the ledger."),
) -> None:
    """Build the brief-set and record a scored ledger under data/bench/."""
    import time as _time

    from skyn3t.studio.bench import built_in_cases, save_run
    console = _console()
    cases = _load_bench_cases(cases_file) or built_in_cases(suite)
    lbl = label or _time.strftime("%Y%m%d-%H%M%S", _time.gmtime())
    console.print(f"[cyan]Benchmark[/cyan] '{lbl}' — building {len(cases)} case(s); "
                  "these are REAL builds and can take a while.")
    run, settings = asyncio.run(_bench_run_async(cases, lbl))
    _print_bench_summary(console, run)
    if not no_save:
        path = save_run(run, settings.data_dir)
        console.print(f"[green]Saved[/green] ledger to [cyan]{path}[/cyan]")


@bench_app.command("publish")
def bench_publish(
    run_path: str = typer.Argument(..., help="Bench run JSON, e.g. data/bench/run-*.json."),
    out: str = typer.Option("", "--out", help="Output directory (default: docs/bench)."),
) -> None:
    """Publish aggregate and per-stack go-rate from a saved bench ledger."""
    from skyn3t.config.settings import REPO_ROOT
    from skyn3t.studio.bench import load_run, publish_go_rate

    console = _console()
    run = load_run(run_path)
    out_dir = Path(out) if out else REPO_ROOT / "docs" / "bench"
    paths = publish_go_rate(run, out_dir)
    console.print(f"[green]Published[/green] go-rate report to [cyan]{paths['markdown']}[/cyan]")
    console.print(f"[dim]Machine summary: {paths['json']}[/dim]")


@audit_app.command("product")
def audit_product(
    out: str = typer.Option(
        "",
        "--out",
        help="Markdown report path (default: docs/audits/<date>-skyn3t-product-audit.md).",
    ),
    json_out: str = typer.Option(
        "",
        "--json-out",
        help="JSON report path (default: <data_dir>/audits/<date>-skyn3t-product-audit.json).",
    ),
    max_findings: int = typer.Option(20, "--max-findings", min=1, help="Maximum findings per audit section."),
    llm: bool = typer.Option(
        True,
        "--llm/--no-llm",
        help="Allow best-effort LLM assistance when a non-stub backend is configured.",
    ),
    include_tests: bool = typer.Option(
        True,
        "--include-tests/--no-include-tests",
        help="Include tests/ in deterministic repo scans.",
    ),
) -> None:
    """Audit SkyN3t itself as an app factory and write one handoff report."""
    from skyn3t.audit import run_product_audit, write_audit_report
    from skyn3t.config.settings import REPO_ROOT, get_settings

    console = _console()
    settings = get_settings()
    stamp = datetime.now(UTC).date().isoformat()
    repo_root = REPO_ROOT
    md_path = Path(out) if out else repo_root / "docs" / "audits" / f"{stamp}-skyn3t-product-audit.md"
    json_path = (
        Path(json_out)
        if json_out
        else settings.data_dir / "audits" / f"{stamp}-skyn3t-product-audit.json"
    )

    llm_client = None
    if llm:
        try:
            from skyn3t.adapters.llm import LLMClient

            candidate = LLMClient(settings)
            if getattr(candidate, "backend", "stub") != "stub":
                llm_client = candidate
        except Exception:  # noqa: BLE001 - audit stays useful offline
            llm_client = None

    report = run_product_audit(
        repo_root=repo_root,
        include_tests=include_tests,
        max_findings=max_findings,
        use_llm=bool(llm_client),
        llm=llm_client,
    )
    md_written, json_written = write_audit_report(report, md_path, json_path)
    console.print(f"[green]Product audit written[/green] {md_written}")
    console.print(f"[green]Audit JSON written[/green] {json_written}")
    console.print(f"Overall rating: {report.overall_rating:.1f}/100")


@bench_app.command("compare")
def bench_compare(
    before: str = typer.Argument(..., help="Baseline run JSON (data/bench/run-*.json)."),
    after: str = typer.Argument(..., help="New run JSON."),
    min_score_delta: float = typer.Option(
        0.0, "--min-score-delta", help="Required mean-score gain to PASS the gate."),
    max_cost_per_go: float = typer.Option(
        -1.0, "--max-cost-per-go", help="Max allowed $/go increase (<0 = ignore cost)."),
) -> None:
    """Diff two runs and render a promotion-gate verdict (exit 1 if rejected)."""
    from skyn3t.studio.bench import diff_runs, gate_change, load_run
    console = _console()
    d = diff_runs(load_run(before), load_run(after))
    cpgd = d.get("cost_per_go_delta")
    console.print(f"go-mean Δ [bold]{d['mean_score_go_delta']:+}[/bold] · "
                  f"mean Δ {d['mean_score_delta']:+} · intent Δ {d['mean_intent_delta']:+} "
                  f"· go-rate Δ {d['go_rate_delta']:+} · "
                  f"$/go Δ {'—' if cpgd is None else f'{cpgd:+.4f}'}")
    for r in d["improvements"]:
        console.print(f"  [green]improved[/green] {r['case_id']} "
                      f"({r['verdict_before']}→{r['verdict_after']})")
    for r in d["regressions"]:
        console.print(f"  [red]REGRESSED[/red] {r['case_id']} "
                      f"({r.get('kind')}: score Δ {r['score_delta']})")
    ok, reasons = gate_change(
        d, min_mean_score_delta=min_score_delta,
        max_cost_per_go_increase=None if max_cost_per_go < 0 else max_cost_per_go)
    if ok:
        console.print("[green]GATE PASS[/green] — measured improvement.")
    else:
        console.print("[red]GATE FAIL[/red] — " + "; ".join(reasons))
        raise typer.Exit(code=1)


@studio_app.command("serve")
def studio_serve(
    project: str = typer.Argument(..., help="Project slug (under Projects/) or an absolute path."),
    port: int = typer.Option(0, "--port", "-p", help="Preferred port (0 = auto)."),
) -> None:
    """Run a generated project as a live local server and print its URL."""
    import time as _time
    from pathlib import Path as _Path

    from skyn3t.config.settings import get_settings
    from skyn3t.studio.app_runner import cleanup_serve
    from skyn3t.studio.preview_supervisor import PreviewSupervisor

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
    runner = PreviewSupervisor()
    app = asyncio.run(runner.start(pdir, stack, port=port or None))
    try:
        if app.status == "no_preview":
            console.print(f"[yellow]No live preview[/yellow] for {pdir} (not a web/site project).")
            raise typer.Exit(code=1)
        if app.status != "running":
            console.print(f"[red]Failed to start[/red]: {app.detail.get('log_tail', '')[-400:]}")
            raise typer.Exit(code=2)
        console.print(
            f"[green]Serving[/green] {pdir.name} at [cyan]{app.url}[/cyan] "
            "(Docker isolated). Press Ctrl+C to stop."
        )
        try:
            while True:
                _time.sleep(1)
        except KeyboardInterrupt:
            console.print("\n[dim]stopped.[/dim]")
    finally:
        try:
            stopped = runner.stop(app)
            if inspect.isawaitable(stopped):
                asyncio.run(stopped)
        except Exception:  # noqa: BLE001 - shutdown must not mask CLI result
            pass
        cleanup_serve(app)


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
    _owned_tmp = False
    if out:
        out_path = out
    else:
        _fd, out_path = _tempfile.mkstemp(prefix="skyn3t-shot-", suffix=".png")
        os.close(_fd)
        _owned_tmp = True
    result = screenshot(url, out_path)
    if result is None:
        if _owned_tmp:
            try:
                os.unlink(out_path)
            except OSError:
                pass
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


async def _run_visual(project: str, *, goal: str, max_rounds: int):
    from pathlib import Path as _Path

    from skyn3t.studio.improve import ImproveEngine
    from skyn3t.studio.preview_supervisor import PreviewSupervisor
    from skyn3t.studio.visual_check import VisualChecker, make_vision_fn
    from skyn3t.studio.visual_loop import visual_self_improve

    spine = await _assemble_spine()
    settings = spine["settings"]
    _l, _p, skills, rag = _build_intelligence(settings, spine["event_bus"], spine["memory"])
    engine = ImproveEngine(spine["event_bus"], spine["orchestrator"],
                           settings=settings, memory=spine["memory"], skills=skills, rag=rag)
    cand = _Path(project)
    pdir = cand if cand.is_absolute() else (settings.projects_dir / project)
    stack = ""
    try:
        from skyn3t.studio.manifest import BuildManifest
        man = BuildManifest.load(pdir)
        stack = man.stack if man else ""
    except Exception:  # noqa: BLE001
        stack = ""
    # Auto-wire the vision judge when an OpenRouter key is configured; otherwise
    # make_vision_fn returns None and the loop soft-skips the judgement step.
    vision_fn = make_vision_fn(settings)
    return await visual_self_improve(
        pdir,
        goal,
        app_runner=PreviewSupervisor(),
        checker=VisualChecker(event_bus=spine["event_bus"]),
        improve_engine=engine, vision_fn=vision_fn, stack=stack, max_rounds=max_rounds)


@studio_app.command("visual")
def studio_visual(
    project: str = typer.Argument(..., help="Project slug (under Projects/) or an absolute path."),
    goal: str = typer.Option(..., "--goal", "-g", help="What the app should look/behave like."),
    rounds: int = typer.Option(2, "--rounds", "-r", help="Max inspect->improve rounds."),
) -> None:
    """Visual self-inspection loop: serve -> screenshot -> judge -> improve -> re-check.

    Needs a vision model wired for the judgement step (soft-skips otherwise);
    Playwright is required for the screenshot."""
    console = _console()
    res = asyncio.run(_run_visual(project, goal=goal, max_rounds=rounds))
    if res.skipped:
        console.print(f"[yellow]Visual loop skipped[/yellow]: {res.reason}")
        raise typer.Exit(code=1)
    tone = "green" if res.passed else "yellow"
    console.print(f"[{tone}]Visual loop {'passed' if res.passed else 'incomplete'}[/{tone}] "
                  f"after {len(res.rounds)} round(s){'' if res.passed else ' — ' + res.reason}")
    for r in res.rounds:
        mark = "[OK]" if r.matches else ("[IMPROVED]" if r.improved else "[X]")
        issues = "" if r.matches else f" | {', '.join(r.issues[:3])}"
        console.print(f"  round {r.index}: {mark}{issues}")
    if not res.passed:
        raise typer.Exit(code=2)


async def _run_liveness_cli(
    project: str,
    *,
    max_rounds: int,
    evidence_dir: str = "",
):
    from pathlib import Path as _Path

    from skyn3t.studio.improve import ImproveEngine
    from skyn3t.studio.liveness import liveness_self_improve
    from skyn3t.studio.preview_supervisor import PreviewSupervisor
    from skyn3t.studio.visual_check import make_vision_fn

    spine = await _assemble_spine()
    settings = spine["settings"]
    _l, _p, skills, rag = _build_intelligence(settings, spine["event_bus"], spine["memory"])
    engine = ImproveEngine(spine["event_bus"], spine["orchestrator"],
                           settings=settings, memory=spine["memory"], skills=skills, rag=rag)
    cand = _Path(project)
    pdir = cand if cand.is_absolute() else (settings.projects_dir / project)
    stack = ""
    try:
        from skyn3t.studio.manifest import BuildManifest
        man = BuildManifest.load(pdir)
        stack = man.stack if man else ""
    except Exception:  # noqa: BLE001
        stack = ""
    return await liveness_self_improve(
        pdir, app_runner=PreviewSupervisor(), improve_engine=engine,
        vision_fn=make_vision_fn(settings), stack=stack, max_rounds=max_rounds,
        evidence_dir=evidence_dir or None)


@studio_app.command("liveness")
def studio_liveness(
    project: str = typer.Argument(..., help="Project slug (under Projects/) or an absolute path."),
    rounds: int = typer.Option(2, "--rounds", "-r", help="Max check->repair rounds."),
    evidence_dir: str = typer.Option(
        "",
        "--evidence-dir",
        help="Output directory for responsive screenshots and JSON (default: project/.skyn3t/visual-proof).",
    ),
    require_visual: bool = typer.Option(
        False,
        "--require-visual",
        help="Exit non-zero when Playwright/Chromium could not produce visual evidence.",
    ),
) -> None:
    """Liveness + responsive proof: serve -> inspect routes/pages -> repair -> re-check.

    Desktop/mobile deterministic checks need Playwright + Chromium but no LLM.
    A configured vision backend adds subjective review to the same evidence."""
    from pathlib import Path as _Path

    console = _console()
    res = asyncio.run(_run_liveness_cli(
        project,
        max_rounds=rounds,
        evidence_dir=evidence_dir,
    ))
    if res.skipped or res.report is None:
        console.print(f"[yellow]Liveness skipped[/yellow]: {res.reason}")
        raise typer.Exit(code=1)
    rep = res.report
    tone = "green" if res.passed else "yellow"
    console.print(f"[{tone}]Liveness {'passed' if res.passed else 'incomplete'}[/{tone}] "
                  f"after {res.rounds} round(s) — {rep.ok}/{rep.total} routes OK "
                  f"(health {rep.health:.0%})")
    visual_total = int(getattr(rep, "visual_total", 0) or 0)
    visual_failed = int(getattr(rep, "visual_failed", 0) or 0)
    visual_skipped = int(getattr(rep, "visual_skipped", 0) or 0)
    if visual_failed:
        visual_status = f"[red]failed[/red] ({visual_failed}/{visual_total} route(s))"
    elif visual_total:
        visual_status = f"[green]passed[/green] ({visual_total} route(s), desktop + mobile)"
    elif visual_skipped:
        visual_status = f"[yellow]skipped[/yellow] ({visual_skipped} route(s))"
    else:
        visual_status = "[dim]not run[/dim]"
    console.print(f"Responsive proof: {visual_status}")
    artifact_dir = getattr(rep, "visual_artifact_dir", None)
    report_path = getattr(rep, "visual_report_path", None)
    if artifact_dir and report_path:
        qualifier = "" if _Path(artifact_dir).is_absolute() else " (project-relative)"
        console.print(
            f"Evidence{qualifier}: [cyan]{_Path(artifact_dir) / report_path}[/cyan]"
        )
    for r in rep.results:
        mark = "[OK]" if r.ok else "[X]"
        if not r.visual:
            vis = ""
        elif r.visual.get("skipped"):
            vis = " | visual skipped"
        else:
            vis = " | visual ok" if r.visual.get("matches") else " | visual failed"
        console.print(f"  {mark} {r.method} {r.path} -> {r.status}{vis}")
    if not res.passed:
        raise typer.Exit(code=2)
    if require_visual and (visual_total == 0 or visual_skipped):
        raise typer.Exit(code=3)


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

    state = AppState(
        settings=settings,
        event_bus=bus,
        orchestrator=spine["orchestrator"],
        memory=spine["memory"],
        studio=studio,
        llm_client=spine["llm"],
        router=spine["router"],
        cortex=cortex,
        skills=getattr(studio, "skills", None),
        patterns=getattr(studio, "patterns", None),
        messaging=messaging,
    )
    ingestor = getattr(rag, "_skyn3t_ingestor", None)
    if ingestor is not None:
        state.ingestors.append(ingestor)
    return state


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
    (``/api/studio/approve`` — the router is mounted under the ``/api`` prefix;
    posting to the bare ``/studio/approve`` hits the SPA catch-all and 405s),
    which mutates the durable build record and resolves the gate on the shared
    spine.

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
        host = str(settings.host or "127.0.0.1").strip()
        connect_host = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
        if ":" in connect_host and not connect_host.startswith("["):
            connect_host = f"[{connect_host}]"
        url = f"http://{connect_host}:{settings.port}/api/studio/approve"
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
                f"(is the control plane running at {connect_host}:{settings.port}?)"
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
        raise typer.Exit(code=1) from exc
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
        raise typer.Exit(code=1) from exc
    n_events = snap.get("published", snap.get("published_count", len(snap.get("history", []))))
    console.print(f"Snapshot written to [cyan]{target}[/cyan] ({n_events} events).")


@app.command()
def deploy(
    project: str = typer.Argument(..., help="A build slug or a path to a delivered project."),
    target: str = typer.Option("", "--target", help="Preferred deploy target (e.g. fly, vercel, cloudflare-pages)."),
    stack: str = typer.Option("", "--stack", help="Override the stack (default: read from the build manifest)."),
    write: bool = typer.Option(False, "--write", help="Write generated deploy artifacts (e.g. a Dockerfile) into the project."),
    now: bool = typer.Option(False, "--now", help="Actually deploy it live (token-gated). Default: just show the plan."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt when using --now."),
) -> None:
    """Show the keyless deploy plan for a build — the right hosts, the exact
    one-command deploy, and (for server stacks) a ready Dockerfile.

    By default nothing is deployed and no token is needed: this is the honest
    "…and here's how it ships" answer for a proven build. Use ``--write`` to drop
    generated artifacts (like a Dockerfile) into the project, or ``--now`` to fire
    a real, token-gated deploy (needs a provider token configured in Settings).
    """
    from pathlib import Path as _Path

    from skyn3t.config.settings import get_settings
    from skyn3t.studio.deploy import (
        apply_deploy_health_gate,
        plan_deploy,
        record_deployment,
        write_deploy_artifacts,
    )

    console = _console()
    s = get_settings()
    cand = _Path(project)
    pdir = cand if cand.is_absolute() else (s.projects_dir / project)
    if not pdir.exists():
        console.print(f"[red]No such build[/red]: {pdir} — pass a slug under "
                      f"[cyan]{s.projects_dir}[/cyan] or an absolute path.")
        raise typer.Exit(code=1)

    # Stack precedence: explicit --stack > the build manifest > content detection
    # (plan_deploy content-detects when the stack is empty/unknown).
    resolved_stack = stack
    man = None
    try:
        from skyn3t.studio.manifest import BuildManifest

        man = BuildManifest.load(pdir)
    except Exception:  # noqa: BLE001 - plan mode can still use content detection
        man = None
    if not resolved_stack:
        resolved_stack = man.stack if man else ""

    plan = plan_deploy(pdir, resolved_stack, target=target or None)

    if not plan.deployable:
        console.print(f"[yellow]No hosted deploy path[/yellow] for "
                      f"[cyan]{pdir.name}[/cyan] (kind: {plan.kind}). {plan.notes}")
        raise typer.Exit(code=0)

    try:
        from rich.table import Table

        table = Table(title=f"Deploy plan — {pdir.name}", show_header=False)
        table.add_row("kind", plan.kind)
        table.add_row("hosts", ", ".join(plan.targets))
        if plan.build_command:
            table.add_row("build", plan.build_command)
        table.add_row("deploy", plan.command)
        table.add_row("output", plan.output_dir)
        table.add_row("serves URL", "yes" if plan.serves_url else "no (a package/binary)")
        if plan.artifacts:
            table.add_row("artifacts", ", ".join(plan.artifacts))
        console.print(table)
    except Exception:  # noqa: BLE001 - rich optional; fall back to plain lines
        console.print(f"kind:   {plan.kind}")
        console.print(f"hosts:  {', '.join(plan.targets)}")
        if plan.build_command:
            console.print(f"build:  {plan.build_command}")
        console.print(f"deploy: {plan.command}")
    console.print(f"[dim]{plan.notes}[/dim]")

    if now:
        from skyn3t.studio.deploy import deployment_quality_gate

        quality = deployment_quality_gate(man)
        if not quality["passed"]:
            console.print(
                "[red]Deploy blocked[/red]: " + "; ".join(quality["blockers"])
            )
            raise typer.Exit(code=1)

    if write and plan.artifacts:
        written = write_deploy_artifacts(plan, pdir)
        if written:
            console.print(f"[green]Wrote[/green] {', '.join(written)} into {pdir}.")
        else:
            console.print("[dim]No artifacts written (already present).[/dim]")

    if not now:
        console.print("[dim]Keyless plan — run the deploy command yourself, or add "
                      "[cyan]--now[/cyan] to deploy it live (needs a provider token in Settings).[/dim]")
        return

    # --now: fire a real, token-gated deploy.
    if not plan.serves_url:
        console.print(f"[yellow]Nothing to serve live[/yellow] — {plan.kind} is a "
                      "package/binary, not a hosted URL. Publish it with: "
                      f"[cyan]{plan.command}[/cyan]")
        raise typer.Exit(code=0)
    # The planner may reject an incompatible requested target and deliberately
    # choose a supported fallback. Execute the command/provider the displayed
    # plan actually selected, never the rejected raw --target value.
    provider = (plan.targets[0] if plan.targets else "").strip()
    if not provider:
        console.print("[red]No deploy target[/red] to deploy to.")
        raise typer.Exit(code=1)
    if not yes and not typer.confirm(f"Deploy {pdir.name} live to {provider}?", default=False):
        console.print("[dim]Aborted — nothing deployed.[/dim]")
        raise typer.Exit(code=0)
    # A container needs its Dockerfile on disk to build the image.
    if plan.artifacts:
        write_deploy_artifacts(plan, pdir)

    from skyn3t.agents.deploy_agent import DeployAgent

    console.print(f"[yellow]Deploying[/yellow] {pdir.name} to [cyan]{provider}[/cyan]…")
    result = DeployAgent().deploy(str(pdir), target=provider, plan=plan)
    deploy_check: dict[str, Any] | None = None

    # A provider command succeeding does not make its URL the active release.
    # When enabled, live health must pass before persistence advances live_url.
    if (
        result.get("ok")
        and result.get("url")
        and getattr(s, "deploy_check_enabled", False)
    ):
        import asyncio as _asyncio

        try:
            from skyn3t.studio.deploy_check import check_deploy

            verdict = _asyncio.run(check_deploy(str(result["url"]), resolved_stack))
            deploy_check = verdict.to_dict()
        except Exception as exc:  # noqa: BLE001 - persist an unverified attempt
            deploy_check = {
                "ok": False,
                "skipped": True,
                "issues": [],
                "checked": {},
                "reason": f"deploy check unavailable: {str(exc)[:160]}",
                "gaps": [],
            }
        result = apply_deploy_health_gate(result, deploy_check)

    deployment_record = record_deployment(
        pdir, result=result, plan=plan, target=provider
    )
    if not deployment_record.get("persisted"):
        console.print(
            "[yellow]Deployment evidence was not persisted[/yellow]: "
            f"{deployment_record.get('persistence_error') or 'unknown manifest error'}"
        )
    if deploy_check:
        if deploy_check.get("ok"):
            console.print("[green]deploy check[/green] — live url verified ✓")
        elif deploy_check.get("skipped"):
            console.print(
                "[yellow]deploy check unverified[/yellow] — "
                f"{deploy_check.get('reason') or 'could not verify live URL'}"
            )
        else:
            issues = deploy_check.get("issues")
            issue_text = "; ".join(str(item) for item in issues[:5]) \
                if isinstance(issues, list) else ""
            console.print(
                f"[red]deploy check failed[/red] — "
                f"{deploy_check.get('reason') or issue_text or 'unhealthy live URL'}"
            )
    if not (result.get("ok") and result.get("url")):
        label = (
            "Deployment activation blocked"
            if result.get("activation_blocked")
            else "Deploy did not complete"
        )
        console.print(
            f"[red]{label}[/red]: {result.get('error') or 'unknown error'}"
        )
        raise typer.Exit(code=1)
    url = result["url"]
    console.print(f"[green]Deployed[/green] at [cyan]{url}[/cyan]")


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
        if hasattr(client, "backend_status"):
            status = client.backend_status()
            state = status.get("state", "ready")
            requested = status.get("requested", getattr(settings, "llm_backend", "auto"))
            detail = f"{backend} (requested {requested}, {state})"
            codegen = (status.get("codegen") or {})
            if codegen.get("cli_provider"):
                detail += f"; codegen={codegen.get('backend')} via {codegen.get('cli_provider')}"
            if status.get("reason"):
                detail += f"; {status['reason']}"
            return (detail, backend != "stub" and state == "ready")
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
