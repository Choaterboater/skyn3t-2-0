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
import inspect
import json
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
async def _assemble_spine(*, with_memory: bool = True) -> dict[str, Any]:
    """Wire event bus, orchestrator, llm, router, memory, and agents.

    Returns a dict of collaborators. Every piece degrades independently.
    """
    from skyn3t.config.settings import get_settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator

    settings = get_settings()
    event_bus = EventBus()

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

    if not web:
        console.print(
            "Spine is ready. Run [cyan]skyn3t studio build \"<brief>\"[/cyan] to build, "
            "or pass [cyan]--web[/cyan] to start the control plane."
        )
        return

    try:
        from skyn3t.web import app as web_app

        if not web_app.fastapi_available():
            raise RuntimeError("fastapi/uvicorn not installed")
        console.print(
            f"Starting web control plane on "
            f"http://{host or settings.host}:{port or settings.port} ..."
        )
        web_app.run(host=host or None, port=port or None)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Web server unavailable:[/red] {exc}")
        console.print("Install with: [cyan]pip install fastapi uvicorn[/cyan]")
        raise typer.Exit(code=1)


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
) -> None:
    """Run a build end to end and print the result + artifact path."""
    console = _console()
    outcome = asyncio.run(_run_build(brief, best_of=best_of, no_critic=no_critic, slug=slug))
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


async def _run_build(brief: str, *, best_of: int, no_critic: bool, slug: str) -> dict[str, Any] | None:
    try:
        from skyn3t.studio.runner import StudioRunner
    except Exception:  # noqa: BLE001
        return None

    spine = await _assemble_spine()
    settings = spine["settings"]
    if no_critic:
        # Per-run override; the runner reads settings.critic_enabled defensively.
        try:
            settings.critic_enabled = False
        except Exception:  # noqa: BLE001
            pass

    runner = StudioRunner(
        spine["event_bus"],
        spine["orchestrator"],
        settings=settings,
        memory=spine["memory"],
    )
    extra: dict[str, Any] = {}
    if best_of and best_of > 1:
        extra["best_of_n"] = best_of
    outcome = await runner.start(brief, slug=slug or None, extra=extra)
    return outcome.to_dict()


@studio_app.command("approve")
def studio_approve(build_id: str = typer.Argument(..., help="Build id to approve.")) -> None:
    """Approve a pending gated build (records the decision as an event)."""
    _decide_build(build_id, approve=True)


@studio_app.command("reject")
def studio_reject(build_id: str = typer.Argument(..., help="Build id to reject.")) -> None:
    """Reject a pending gated build (records the decision as an event)."""
    _decide_build(build_id, approve=False)


def _decide_build(build_id: str, *, approve: bool) -> None:
    """Persist an approval/rejection as a DECIDED event.

    Gated builds run inside a live process; out-of-band approval is recorded as
    an event so any attached process (or the next snapshot) reflects the
    decision. We never crash if no spine is live.
    """
    console = _console()

    async def _emit() -> None:
        from skyn3t.core.events import EventBus, EventType

        bus = EventBus()
        await bus.emit(
            EventType.PROPOSAL_DECIDED,
            "cli",
            {
                "build_id": build_id,
                "decision": "approved" if approve else "rejected",
                "decided_by": "cli",
            },
        )

    try:
        asyncio.run(_emit())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]Could not record decision:[/red] {exc}")
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
    from skyn3t.core.events import EventBus

    settings = get_settings()
    bus = EventBus()
    snap = bus.snapshot()
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


async def _fetch_url(url: str) -> str | None:
    try:
        import httpx
    except Exception:  # noqa: BLE001 - httpx is optional
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception:  # noqa: BLE001
        return None


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
