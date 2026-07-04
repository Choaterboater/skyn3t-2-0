"""CodeImproverAgent — creating a missing imported file, not just editing existing
ones (roadmap: fix the shared dangling-import bug found via a real deepseek build,
where `src/main.js` imported `./PreloadScene.js` that codegen never wrote — the app
couldn't boot, and the repair loop had no way to create the missing file, only edit
files that already exist).

Root-cause detail these tests pin: the "UNRESOLVED IMPORT" gap format is
"<importer> -> <spec>" where `spec` is relative to the IMPORTER's own directory, not
the worktree root (e.g. "src/main.js -> ./PreloadScene.js" means
"src/PreloadScene.js", not a bare "./PreloadScene.js" resolved at the worktree
root). A naive regex extraction of file-looking tokens from the gap text gets this
wrong; the fix must resolve relative to the importer.
"""
from __future__ import annotations

from unittest.mock import patch

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.core.agent import TaskRequest
from skyn3t.core.events import EventBus


def _make_agent():
    bus = EventBus()
    return CodeImproverAgent(event_bus=bus)


def _as_llm_cli(agent):
    """Mirrors the pattern used elsewhere in this codebase (test_code_agent_retry.py)
    for making an agent's LLMClient report a non-stub backend without a real key."""
    agent.llm._backend = "claude_cli"
    return patch.object(type(agent.llm), "backend", new_callable=lambda: property(
        lambda self: getattr(self, "_backend", "stub")
    ))


async def test_creates_missing_file_at_the_importer_relative_path_not_worktree_root(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.js").write_text(
        "import { PreloadScene } from './PreloadScene.js';\n"
        "export default PreloadScene;\n")
    improver = _make_agent()
    await improver.start()

    captured = {}

    async def fake_complete(prompt, **kw):
        from skyn3t.adapters.llm import LLMResult
        captured["prompt"] = prompt
        return LLMResult(text="export class PreloadScene {}\n", model="fake",
                         backend="claude_cli")

    with _as_llm_cli(improver):
        improver.llm.complete = fake_complete
        result = await improver.run(TaskRequest(
            type="code_improve",
            payload={"brief": "a phaser game", "worktree_dir": str(tmp_path),
                     "extra": {"role_guidance": "Use the imported game repair role."},
                     "gaps": ["UNRESOLVED IMPORT — create the missing target or fix "
                              "the path: src/main.js -> ./PreloadScene.js"]}))

    assert result.success
    assert "src/PreloadScene.js" in result.output["files"]
    assert not (tmp_path / "PreloadScene.js").exists()  # NOT at the worktree root
    created = (src / "PreloadScene.js").read_text()
    assert "PreloadScene" in created
    # The importer's own content must have been fed as context, not just the spec.
    assert "src/main.js" in captured["prompt"]
    assert "PreloadScene" in captured["prompt"]
    assert "STAGE ROLE GUIDANCE" in captured["prompt"]
    assert "imported game repair role" in captured["prompt"]


async def test_stub_backend_does_not_fabricate_a_missing_file(tmp_path):
    # Default test-env backend is "stub" (conftest.py pins SKYN3T_LLM_BACKEND=stub).
    # There is no deterministic "create from nothing" fallback -- a stub backend
    # must leave the missing file absent, not write nonsense content.
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.js").write_text("import { PreloadScene } from './PreloadScene.js';\n")
    improver = _make_agent()
    await improver.start()
    result = await improver.run(TaskRequest(
        type="code_improve",
        payload={"brief": "a phaser game", "worktree_dir": str(tmp_path),
                 "gaps": ["UNRESOLVED IMPORT — create the missing target or fix "
                          "the path: src/main.js -> ./PreloadScene.js"]}))
    assert result.success
    assert not (src / "PreloadScene.js").exists()


async def test_create_never_raises_when_llm_explodes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.js").write_text("import { PreloadScene } from './PreloadScene.js';\n")
    improver = _make_agent()
    await improver.start()

    async def exploding_complete(prompt, **kw):
        raise RuntimeError("provider timeout")

    with _as_llm_cli(improver):
        improver.llm.complete = exploding_complete
        result = await improver.run(TaskRequest(
            type="code_improve",
            payload={"brief": "a phaser game", "worktree_dir": str(tmp_path),
                     "gaps": ["UNRESOLVED IMPORT — create the missing target or fix "
                              "the path: src/main.js -> ./PreloadScene.js"]}))
    assert result.success  # never breaks the whole task
    assert not (src / "PreloadScene.js").exists()


async def test_existing_files_are_still_edited_not_recreated(tmp_path):
    # Regression guard: the create-path change must not affect the pre-existing
    # edit-existing-file behavior.
    src = tmp_path / "src"
    src.mkdir()
    (src / "App.jsx").write_text("function App() { return null }\n")
    improver = _make_agent()
    await improver.start()
    result = await improver.run(TaskRequest(
        type="code_improve",
        payload={"brief": "react app", "worktree_dir": str(tmp_path),
                 "gaps": ["src/App.jsx is missing a default export"]}))
    assert result.success
    assert "export default" in (src / "App.jsx").read_text()


async def test_create_path_respects_worktree_confinement(tmp_path):
    # A gap naming a path outside the worktree must never be created, mirroring the
    # existing _confined() guard that already protects the edit path.
    (tmp_path / "src").mkdir()
    improver = _make_agent()
    await improver.start()
    with _as_llm_cli(improver):
        async def fake_complete(prompt, **kw):
            from skyn3t.adapters.llm import LLMResult
            return LLMResult(text="whatever", model="fake", backend="claude_cli")
        improver.llm.complete = fake_complete
        result = await improver.run(TaskRequest(
            type="code_improve",
            payload={"brief": "x", "worktree_dir": str(tmp_path),
                     "files": ["../../etc/evil.js"]}))
    assert result.success
    assert not (tmp_path.parent.parent / "etc" / "evil.js").exists()


# ---------------------------------------------------------------------------
# Target discovery for free-text improve goals (the "Improve silently does
# nothing" bug): ImproveEngine._run_improver() never sets payload["files"], and
# a free-text goal like "add a contact form" matches none of
# _targets_from_gaps()'s error-shaped regexes, so it fell through to a
# React-Vite-only entrypoint guess list. A real delivered Next.js App Router
# project (app/page.jsx, no src/App.jsx) got targets == [] -> the edit loop
# never ran -> zero files touched, invisible to the user.
# ---------------------------------------------------------------------------

def test_targets_from_gaps_finds_nextjs_app_router_entrypoint(tmp_path):
    # Exact reproduction shape of the real broken project: App Router, no
    # src/App.jsx (the only entrypoint the old guess list knew about).
    app = tmp_path / "app"
    app.mkdir()
    (app / "page.jsx").write_text("export default function Page() { return null }\n")
    (app / "layout.jsx").write_text("export default function Layout({children}) { return children }\n")
    improver = _make_agent()
    targets = improver._targets_from_gaps(
        ["add a contact form and improve the SEO metadata"], tmp_path)
    assert targets, "must not be empty for a real Next.js App Router project"
    assert "app/page.jsx" in targets


def test_targets_from_gaps_finds_nextjs_pages_router_entrypoint(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "index.jsx").write_text("export default function Home() { return null }\n")
    improver = _make_agent()
    targets = improver._targets_from_gaps(["make the hero section bigger"], tmp_path)
    assert "pages/index.jsx" in targets


def test_targets_from_gaps_finds_astro_entrypoint(tmp_path):
    pages = tmp_path / "src" / "pages"
    pages.mkdir(parents=True)
    (pages / "index.astro").write_text("<html></html>\n")
    improver = _make_agent()
    targets = improver._targets_from_gaps(["fix the footer copy"], tmp_path)
    assert "src/pages/index.astro" in targets


def test_targets_from_gaps_finds_remix_entrypoint(tmp_path):
    routes = tmp_path / "app" / "routes"
    routes.mkdir(parents=True)
    (routes / "_index.jsx").write_text("export default function Index() { return null }\n")
    (tmp_path / "app" / "root.jsx").write_text("export default function Root() { return null }\n")
    improver = _make_agent()
    targets = improver._targets_from_gaps(["add analytics"], tmp_path)
    assert "app/routes/_index.jsx" in targets


async def test_llm_target_discovery_used_when_gaps_yield_no_targets(tmp_path):
    # A SvelteKit-style layout isn't covered by ANY entrypoint guess -- the
    # deterministic fallback must come up empty, forcing the LLM-driven
    # repo-map discovery path to run.
    routes = tmp_path / "src" / "routes"
    routes.mkdir(parents=True)
    (routes / "+page.svelte").write_text("<h1>Home</h1>\n")
    lib = tmp_path / "src" / "lib"
    lib.mkdir()
    (lib / "Header.svelte").write_text("<header></header>\n")

    improver = _make_agent()
    assert improver._targets_from_gaps(["add a contact form"], tmp_path) == []
    await improver.start()

    captured = {}

    async def fake_complete(prompt, **kw):
        from skyn3t.adapters.llm import LLMResult
        if "Repository map" in prompt:
            captured["discovery_prompt"] = prompt
            # Mix a real file, a hallucinated file that doesn't exist, and a
            # path trying to escape the worktree -- only the real, confined
            # file must survive.
            return LLMResult(
                text="src/routes/+page.svelte\nsrc/lib/DoesNotExist.svelte\n"
                     "../../etc/passwd\n",
                model="fake", backend="claude_cli")
        # The subsequent per-file rewrite call for the one surviving target.
        return LLMResult(text="<h1>Contact us</h1>\n", model="fake", backend="claude_cli")

    with _as_llm_cli(improver):
        improver.llm.complete = fake_complete
        result = await improver.run(TaskRequest(
            type="code_improve",
            payload={"brief": "add a contact form", "worktree_dir": str(tmp_path),
                     "gaps": ["add a contact form"],
                     "repo_map": "src/routes/+page.svelte\nsrc/lib/Header.svelte\n"}))

    assert result.success
    assert result.output["files"] == ["src/routes/+page.svelte"]
    assert not (tmp_path.parent.parent / "etc" / "passwd").exists()
    assert "src/routes/+page.svelte" in captured["discovery_prompt"]
    assert "add a contact form" in captured["discovery_prompt"]


async def test_llm_target_discovery_skipped_on_stub_backend(tmp_path):
    # Default test-env backend is "stub" (conftest.py pins SKYN3T_LLM_BACKEND=stub).
    # No repo_map-driven LLM call should happen, and the task must still succeed
    # with zero files touched rather than raising.
    routes = tmp_path / "src" / "routes"
    routes.mkdir(parents=True)
    (routes / "+page.svelte").write_text("<h1>Home</h1>\n")
    improver = _make_agent()
    await improver.start()
    result = await improver.run(TaskRequest(
        type="code_improve",
        payload={"brief": "add a contact form", "worktree_dir": str(tmp_path),
                 "gaps": ["add a contact form"],
                 "repo_map": "src/routes/+page.svelte\n"}))
    assert result.success
    assert result.output["files"] == []


async def test_llm_target_discovery_never_raises_when_llm_explodes(tmp_path):
    routes = tmp_path / "src" / "routes"
    routes.mkdir(parents=True)
    (routes / "+page.svelte").write_text("<h1>Home</h1>\n")
    improver = _make_agent()
    await improver.start()

    async def exploding_complete(prompt, **kw):
        raise RuntimeError("provider timeout")

    with _as_llm_cli(improver):
        improver.llm.complete = exploding_complete
        result = await improver.run(TaskRequest(
            type="code_improve",
            payload={"brief": "add a contact form", "worktree_dir": str(tmp_path),
                     "gaps": ["add a contact form"],
                     "repo_map": "src/routes/+page.svelte\n"}))
    assert result.success  # never breaks the whole task
    assert result.output["files"] == []


async def test_llm_target_discovery_skipped_when_no_repo_map(tmp_path):
    # No repo_map in the payload at all -- must not attempt an LLM call, and
    # must not raise; degrade to whatever the deterministic fallback found
    # (nothing, for this unconventional layout).
    routes = tmp_path / "src" / "routes"
    routes.mkdir(parents=True)
    (routes / "+page.svelte").write_text("<h1>Home</h1>\n")
    improver = _make_agent()
    await improver.start()
    with _as_llm_cli(improver):
        async def fake_complete(prompt, **kw):
            raise AssertionError("must not call the LLM without a repo_map")
        improver.llm.complete = fake_complete
        result = await improver.run(TaskRequest(
            type="code_improve",
            payload={"brief": "add a contact form", "worktree_dir": str(tmp_path),
                     "gaps": ["add a contact form"]}))
    assert result.success
    assert result.output["files"] == []
