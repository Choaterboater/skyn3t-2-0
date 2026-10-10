"""code_degraded must reflect the DELIVERED tree, not a codegen-time snapshot.

bellwork-v2 (2026-10-09): the stall guard cut Opus off mid-slice, so codegen
recorded "missing 1 owned file(s): app/globals.css". The fix-loop then wrote
app/globals.css, `next build` passed, the app rendered — and the build was still
no_go because the degraded flag was never re-checked against the disk."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skyn3t.agents._scaffold import scaffold_for
from skyn3t.agents.code_agent import CodeAgent
from skyn3t.config.settings import Settings
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.planner import BuildPlan
from skyn3t.studio.proof_run import scaffold_missing_imports
from skyn3t.studio.runner import StudioRunner, degraded_reason_resolved

_REASON = ("frontend: agentic slice 'frontend' incomplete after 1 retry: provider did "
           "not confirm completion: agentic CLI ended without a successful result; "
           "missing 1 owned file(s): app/globals.css")


def test_resolved_when_every_missing_file_now_exists(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "globals.css").write_text(":root{--bg:#fff}\n")
    assert degraded_reason_resolved(_REASON, tmp_path) is True


def test_unresolved_while_file_still_missing_or_empty(tmp_path):
    assert degraded_reason_resolved(_REASON, tmp_path) is False
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "globals.css").write_text("  \n")
    assert degraded_reason_resolved(_REASON, tmp_path) is False


def test_multi_slice_all_must_resolve(tmp_path):
    reason = (_REASON + "; backend: completion slice 'backend' missed 2 owned file(s): "
              "lib/a.js, lib/b.js")
    (tmp_path / "app").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "app" / "globals.css").write_text("a{}\n")
    (tmp_path / "lib" / "a.js").write_text("export const a=1\n")
    assert degraded_reason_resolved(reason, tmp_path) is False
    (tmp_path / "lib" / "b.js").write_text("export const b=1\n")
    assert degraded_reason_resolved(reason, tmp_path) is True


def test_truncated_list_cannot_be_verified(tmp_path):
    names = [f"f{i}.js" for i in range(12)]
    for n in names:
        (tmp_path / n).write_text("x=1\n")
    reason = "slice 's' incomplete: missing 14 owned file(s): " + ", ".join(names)
    assert degraded_reason_resolved(reason, tmp_path) is False


def test_non_file_reasons_never_resolve(tmp_path):
    for reason in ("agentic build under-delivered after 1 retry",
                   "frontend: slice produced no files",
                   "frontend: agentic slice 'frontend' incomplete after 1 retry: "
                   "provider did not confirm completion",
                   "x: missing 0 owned file(s): ; rejected prose source: app/page.jsx",
                   ""):
        assert degraded_reason_resolved(reason, tmp_path) is False, reason


# The monolithic agentic path (the CLI default under best-of-N) and the completion
# path word their reasons differently from slices: "planned" files and an
# "agentic build failed or did not confirm completion" note (code_agent.py). The
# resolver only understood the slice wording, so these never cleared.
_MONOLITHIC = ("agentic build incomplete after 1 retry: agentic build failed or did not "
               "confirm completion: agentic CLI ended without a successful result; "
               "missing 2 planned file(s): src/App.jsx, src/styles.css")


def test_monolithic_reason_resolves_once_planned_files_exist(tmp_path):
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path, scaffold_sha256={}) is False
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export default function App(){return null}\n")
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path, scaffold_sha256={}) is False
    (tmp_path / "src" / "styles.css").write_text("body{margin:0}\n")
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path, scaffold_sha256={}) is True


def test_monolithic_confirmed_but_missing_planned_resolves(tmp_path):
    reason = "agentic build incomplete after 1 retry: missing 1 planned file(s): app/page.tsx"
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("export default function Page(){return null}\n")
    assert degraded_reason_resolved(reason, tmp_path, scaffold_sha256={}) is True


def test_completion_codegen_missed_planned_resolves(tmp_path):
    reason = "completion codegen missed 1 planned file(s): lib/db.js"
    assert degraded_reason_resolved(reason, tmp_path, scaffold_sha256={}) is False
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "db.js").write_text("export const db = {}\n")
    assert degraded_reason_resolved(reason, tmp_path, scaffold_sha256={}) is True


def test_monolithic_non_file_reasons_still_never_resolve(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("x\n")
    for reason in (
        # unconfirmed completion alone: nothing on disk to verify
        "agentic build incomplete after 1 retry: agentic build failed or did not "
        "confirm completion: provider returned without a successful finish",
        "agentic build incomplete after 1 retry: agentic build failed or did not "
        "confirm completion: timeout; missed required react contract: src/main.jsx",
        "agentic build incomplete after 2 retries: under-delivered 120 code bytes in "
        "1 files (threshold 4000)",
        "agentic build incomplete after 1 retry: missing 14 planned file(s): src/App.jsx",
    ):
        assert degraded_reason_resolved(reason, tmp_path, scaffold_sha256={}) is False, reason


# Monolithic/completion codegen computes the reason BEFORE it back-fills missing
# planned paths with the offline scaffold, so a "missing" scaffold path (main.py,
# src/App.jsx, src/sim.js ...) holds placeholder content the moment codegen ends.
# Existence alone would clear the gate with zero repair work; the code stage's
# ``scaffold_filled`` record (sha256 of the delivered bytes) tells them apart.
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_planned_path_still_holding_scaffold_bytes_never_resolves(tmp_path):
    (tmp_path / "src").mkdir()
    app = tmp_path / "src" / "App.jsx"
    app.write_text("export default function App(){return 'placeholder'}\n")
    (tmp_path / "src" / "styles.css").write_text("body{margin:0}\n")
    record = {"src/App.jsx": _sha(app)}
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path, scaffold_sha256=record) is False
    # The fix-loop rewrote it: different bytes, so the defect is really repaired.
    app.write_text("export default function App(){return 'real app'}\n")
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path, scaffold_sha256=record) is True


def test_planned_reason_without_scaffold_record_fails_closed(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export default function App(){return 1}\n")
    (tmp_path / "src" / "styles.css").write_text("body{margin:0}\n")
    assert degraded_reason_resolved(_MONOLITHIC, tmp_path) is False
    assert degraded_reason_resolved(
        "completion codegen missed 1 planned file(s): src/App.jsx", tmp_path) is False
    # Slices never back-fill from the scaffold: their reasons need no record.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "globals.css").write_text(":root{--bg:#fff}\n")
    assert degraded_reason_resolved(_REASON, tmp_path) is True


_BIG_JS = "\n".join(f"export const value_{i} = {i};" for i in range(1400))


class _ScriptedAgenticLLM:
    backend = "claude_cli"
    supports_agentic = True
    last_model = None
    last_route = None
    routes: list = []

    def __init__(self, files: dict[str, str]):
        self.files = files

    async def agentic_build(self, prompt, workdir, **kwargs):
        for rel, content in self.files.items():
            target = Path(workdir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return {"ok": True, "completed": True, "backend": "claude_cli"}


async def _codegen(tmp_path, stack: str, planned: list[str], llm) -> dict:
    agent = CodeAgent(event_bus=EventBus(), llm=llm)
    await agent.start()
    result = await agent.run(TaskRequest(
        type="codegen",
        payload={
            "brief": "an app", "slug": "app", "stack": stack,
            "plan": {"stack": stack, "files": [{"path": p} for p in planned]},
            "worktree_dir": str(tmp_path),
        },
        capabilities_required=("codegen",),
    ))
    assert result.success
    return result.output


@pytest.mark.parametrize(("stack", "missing", "written"), [
    ("react_vite", ["src/App.jsx", "src/styles.css"], {"src/feature.js": _BIG_JS}),
    ("fastapi", ["main.py", "test_main.py"],
     {"app/core.py": "\n".join(f"VALUE_{i} = {i}" for i in range(1400))}),
])
async def test_monolithic_scaffold_backfill_is_recorded_and_never_self_resolves(
        tmp_path, stack, missing, written):
    out = await _codegen(tmp_path, stack, [*missing, *written],
                         _ScriptedAgenticLLM(written))
    reason = out["degraded_reason"]
    assert f"missing {len(missing)} planned file(s): {', '.join(missing)}" in reason
    record = out["scaffold_filled"]
    for rel in missing:
        # Back-filled from the scaffold: present, and recorded with its bytes.
        assert record[rel] == _sha(tmp_path / rel)
    for rel in written:
        assert rel not in record
    assert degraded_reason_resolved(reason, tmp_path, scaffold_sha256=record) is False
    # Only after the fix-loop actually rewrites every named file does it clear.
    for rel in missing:
        (tmp_path / rel).write_text(f"# repaired {rel}\nREAL = True\n")
    assert degraded_reason_resolved(reason, tmp_path, scaffold_sha256=record) is True


async def test_repair_stub_in_a_missing_planned_path_is_recorded(tmp_path):
    """_repair_entrypoints stubs an imported-but-missing stylesheet after the
    reason is written; that stand-in must not clear the gate either."""
    main_jsx = "import './index.css'\nimport App from './App.jsx'\n" + _BIG_JS
    out = await _codegen(tmp_path, "react_vite", ["src/main.jsx", "src/index.css"],
                         _ScriptedAgenticLLM({"src/main.jsx": main_jsx}))
    reason = out["degraded_reason"]
    assert "missing 1 planned file(s): src/index.css" in reason
    assert "stub stylesheet" in (tmp_path / "src" / "index.css").read_text()
    assert out["scaffold_filled"]["src/index.css"] == _sha(tmp_path / "src" / "index.css")
    assert degraded_reason_resolved(
        reason, tmp_path, scaffold_sha256=out["scaffold_filled"]) is False


async def test_completion_scaffold_fallback_is_recorded(tmp_path):
    class _CompletionLLM:
        backend = "openrouter"
        supports_agentic = False
        last_model = None
        last_route = None
        routes: list = []

    agent = CodeAgent(event_bus=EventBus(), llm=_CompletionLLM())  # type: ignore[arg-type]
    await agent.start()

    async def fake_generate(rel_path, *args, **kwargs):
        if rel_path == "src/App.jsx":
            raise RuntimeError("provider timed out")
        return f"// generated {rel_path}\n{_BIG_JS}\n"

    agent._generate_file = fake_generate  # type: ignore[method-assign]
    result = await agent.run(TaskRequest(
        type="codegen",
        payload={
            "brief": "an app", "slug": "app", "stack": "react_vite",
            "plan": {"stack": "react_vite",
                     "files": [{"path": "src/App.jsx"}, {"path": "src/feature.js"}]},
            "worktree_dir": str(tmp_path),
        },
        capabilities_required=("codegen",),
    ))
    out = result.output
    assert out["degraded_reason"] == "completion codegen missed 1 planned file(s): src/App.jsx"
    assert out["scaffold_filled"]["src/App.jsx"] == _sha(tmp_path / "src" / "App.jsx")
    assert "src/feature.js" not in out["scaffold_filled"]
    assert degraded_reason_resolved(
        out["degraded_reason"], tmp_path, scaffold_sha256=out["scaffold_filled"]) is False


async def test_phaser_contract_gap_is_not_hidden_by_missing_planned(tmp_path):
    main_js = "import Phaser from 'phaser';\nnew Phaser.Game({});\n" + _BIG_JS
    out = await _codegen(
        tmp_path, "phaser", ["src/main.js", "src/sim.js", "src/scenes/Game.js"],
        _ScriptedAgenticLLM({"src/main.js": main_js, "src/scenes/Game.js": _BIG_JS}))
    reason = out["degraded_reason"]
    assert "missing 1 planned file(s): src/sim.js" in reason
    assert "missed required phaser contract" in reason
    (tmp_path / "src" / "sim.js").write_text("export function step(s){return s}\n")
    assert degraded_reason_resolved(
        reason, tmp_path, scaffold_sha256=out["scaffold_filled"]) is False


async def test_under_delivery_is_not_hidden_by_missing_planned(tmp_path):
    out = await _codegen(
        tmp_path, "react_vite", ["src/feature.js", "src/extra.js"],
        _ScriptedAgenticLLM({"src/feature.js": "export const a = 1\n"}))
    reason = out["degraded_reason"]
    assert "missing 1 planned file(s): src/extra.js" in reason
    assert "under-delivered" in reason
    (tmp_path / "src" / "extra.js").write_text("export const b = 2\n")
    assert degraded_reason_resolved(
        reason, tmp_path, scaffold_sha256=out["scaffold_filled"]) is False


# The RUNNER also writes stand-ins into missing paths before the gate is checked:
# the pre-proof deterministic repairs stub missing import targets (an
# "Auto-scaffolded by SkyN3t" module or the stub stylesheet) and the fix-loop's
# _fill_missing copies its own scaffold or a _stub_for placeholder. Each lets the
# proof pass, none is delivery — for slice ("owned") and "planned" reasons alike.
def _planned(*paths: str) -> str:
    return (f"agentic build incomplete after 1 retry: missing {len(paths)} planned "
            f"file(s): {', '.join(paths)}")


def _owned(*paths: str) -> str:
    return (f"frontend: agentic slice 'frontend' incomplete after 1 retry: missing "
            f"{len(paths)} owned file(s): {', '.join(paths)}")


def _runner() -> StudioRunner:
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def test_import_stub_at_a_missing_path_never_resolves(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text(
        "import Board from './components/Board';\n"
        "export default function App(){return <Board/>}\n")
    assert scaffold_missing_imports(tmp_path, stack="react_vite") == [
        "src/components/Board.jsx"]
    board = tmp_path / "src" / "components" / "Board.jsx"
    assert "Auto-scaffolded by SkyN3t" in board.read_text()
    for reason, record in ((_planned("src/components/Board.jsx"), {}),
                           (_owned("src/components/Board.jsx"), None)):
        assert degraded_reason_resolved(reason, tmp_path, scaffold_sha256=record) is False
    board.write_text("export default function Board(){return <section>board</section>}\n")
    assert degraded_reason_resolved(
        _planned("src/components/Board.jsx"), tmp_path, scaffold_sha256={}) is True


def test_stub_stylesheet_never_resolves(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.jsx").write_text(
        "import './index.css'\nimport App from './App.jsx'\n")
    assert "src/index.css" in scaffold_missing_imports(tmp_path, stack="react_vite")
    for reason, record in ((_planned("src/index.css"), {}), (_owned("src/index.css"), None)):
        assert degraded_reason_resolved(reason, tmp_path, scaffold_sha256=record) is False
    (tmp_path / "src" / "index.css").write_text("body{margin:0}\n")
    assert degraded_reason_resolved(
        _planned("src/index.css"), tmp_path, scaffold_sha256={}) is True


def test_smoke_test_stub_never_resolves(tmp_path):
    plan = BuildPlan(slug="tool", brief="a tool", stack="python_cli", stages=[])
    assert _runner()._fill_missing(str(tmp_path), plan, "a tool", ["tests/test_tool.py"]) == 1
    test_file = tmp_path / "tests" / "test_tool.py"
    assert test_file.read_text() == StudioRunner._stub_for("tests/test_tool.py", plan, "a tool")
    reason = "backend: completion slice 'backend' missed 1 owned file(s): tests/test_tool.py"
    assert degraded_reason_resolved(reason, tmp_path) is False
    test_file.write_text("def test_smoke() :\r\n\tassert   True")  # whitespace only differs
    assert degraded_reason_resolved(reason, tmp_path) is False
    test_file.write_text("from tool import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    assert degraded_reason_resolved(reason, tmp_path) is True


def test_fill_missing_scaffold_at_a_missing_source_path_never_resolves(tmp_path):
    brief = "a kanban board"
    plan = BuildPlan(slug="board", brief=brief, stack="react", stages=[])
    missing = ["src/App.jsx", "src/main.jsx", "index.html"]
    assert _runner()._fill_missing(str(tmp_path), plan, brief, missing) == 3
    digests = StudioRunner._runner_scaffold_sha256(plan, brief)
    assert set(missing) <= set(digests)
    for rel in missing:
        assert digests[rel] == _sha(tmp_path / rel)
        # The runner's back-fill alone (codegen recorded nothing) keeps the gate.
        for reason, record in ((_planned(rel), {}), (_owned(rel), None)):
            assert degraded_reason_resolved(
                reason, tmp_path, scaffold_sha256=record,
                runner_scaffold_sha256=digests) is False, (rel, reason)
    for rel in missing:
        (tmp_path / rel).write_text(f"// repaired {rel}\nexport const real = true\n")
    assert degraded_reason_resolved(
        _planned(*missing), tmp_path, scaffold_sha256={},
        runner_scaffold_sha256=digests) is True


def test_fill_missing_scaffold_stylesheet_still_resolves(tmp_path):
    """bellwork: the fix-loop wrote app/globals.css from the scaffold and the app
    built. A stylesheet's scaffold copy is a working file, not a placeholder."""
    brief = "a classroom bellwork app"
    plan = BuildPlan(slug="bellwork", brief=brief, stack="nextjs", stages=[])
    assert _runner()._fill_missing(str(tmp_path), plan, brief, ["app/globals.css"]) == 1
    digests = StudioRunner._runner_scaffold_sha256(plan, brief)
    assert digests["app/globals.css"] == _sha(tmp_path / "app" / "globals.css")
    assert degraded_reason_resolved(
        _REASON, tmp_path, runner_scaffold_sha256=digests) is True


def test_boilerplate_scaffold_copies_are_not_placeholders(tmp_path):
    """Config, manifests and stylesheets whose scaffold copy is already a correct
    delivery must not pin the gate shut once proof passed (no false no_go); source
    modules and index.html holding scaffold bytes still do."""
    scaffold = scaffold_for("react_vite", "app", "an app")
    boilerplate = ["vite.config.js", ".gitignore", "package.json", "README.md",
                   "src/styles.css", "src/design-tokens.css"]
    placeholders = ["src/App.jsx", "src/main.jsx", "index.html"]
    for rel in boilerplate + placeholders:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(scaffold[rel])
    record = {rel: _sha(tmp_path / rel) for rel in boilerplate + placeholders}
    for kwargs in ({"scaffold_sha256": record},
                   {"scaffold_sha256": {}, "runner_scaffold_sha256": record}):
        assert degraded_reason_resolved(_planned(*boilerplate), tmp_path, **kwargs) is True
        for rel in placeholders:
            assert degraded_reason_resolved(
                _planned(*boilerplate, rel), tmp_path, **kwargs) is False, rel


def test_real_content_at_a_scaffold_path_resolves(tmp_path):
    scaffold = scaffold_for("react_vite", "app", "an app")
    record = {rel: hashlib.sha256(c.encode()).hexdigest() for rel, c in scaffold.items()}
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text(
        "export default function App(){return <main>kanban</main>}\n")
    (tmp_path / "src" / "Board.jsx").write_text(
        "export default function Board(){return <section/>}\n")
    assert degraded_reason_resolved(
        _planned("src/App.jsx", "src/Board.jsx"), tmp_path,
        scaffold_sha256=record, runner_scaffold_sha256=record) is True
