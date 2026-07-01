"""SEO gate wiring: source-file selection + the advisory repair dispatch/rollback path.

The advisory SEO stage may hand delivered pages to the code_improver AFTER proof/liveness/
verdict have run. A broken rewrite must NEVER survive into the delivered tree (do-no-harm):
these tests pin which files are selected, the ``.html`` validation + script-entrypoint
guards that protect a rewrite, and the runner's dispatch -> re-proof -> keep-or-rollback
contract. Harness mirrors tests/test_fix_loop.py + tests/test_liveness_runner.py: a real
Orchestrator with a registered fake code_improve agent, and ``proof_run`` monkeypatched on
the runner module.
"""
from __future__ import annotations

from pathlib import Path

from skyn3t.agents.code_improver import CodeImproverAgent
from skyn3t.agents.validate import validate_source
from skyn3t.config.settings import Settings
from skyn3t.core.agent import AgentCapability, BaseAgent, TaskRequest, TaskResult
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio import runner as runner_mod
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.planner import Planner
from skyn3t.studio.runner import StudioRunner

BARE_HTML = "<!DOCTYPE html><html><head></head><body><p>hi</p></body></html>"
FULL_HTML = (
    '<!DOCTYPE html><html lang="en"><head><title>Cool Site</title>'
    '<meta name="description" content="A genuinely cool static site about things" />'
    '<meta property="og:title" content="Cool Site" />'
    '<meta property="og:description" content="A cool static site about things" />'
    "</head><body><h1>Cool Site</h1></body></html>"
)


def _runner():
    bus = EventBus()
    return StudioRunner(bus, Orchestrator(bus), settings=Settings(llm_backend="stub"))


def _touch(root, rel):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")


# ── FIX 4: _select_seo_source_files ──────────────────────────────────────────
def test_select_seo_files_accepts_page_and_meta_sources(tmp_path):
    for rel in ("index.html", "app/layout.tsx", "app/page.jsx",
                "src/app/layout.tsx", "pages/_document.tsx", "landing.astro"):
        _touch(tmp_path, rel)
    picks = set(StudioRunner._select_seo_source_files(str(tmp_path)))
    assert picks == {
        "index.html", "app/layout.tsx", "app/page.jsx",
        "src/app/layout.tsx", "pages/_document.tsx", "landing.astro",
    }


def test_select_seo_files_accepts_remix_roots(tmp_path):
    _touch(tmp_path, "app/root.tsx")
    _touch(tmp_path, "app/routes/_index.tsx")
    picks = set(StudioRunner._select_seo_source_files(str(tmp_path)))
    assert "app/root.tsx" in picks
    assert "app/routes/_index.tsx" in picks


def test_select_seo_files_excludes_build_artifacts_and_non_pages(tmp_path):
    for rel in ("app/page.module.css", "node_modules/pkg/index.html",
                ".next/server/index.html", "dist/index.html", "out/page.html",
                "build/index.html", ".output/index.html", ".preview/index.html",
                "components/Button.tsx", "lib/util.ts", "styles/app.css",
                "app/api/route.ts"):
        _touch(tmp_path, rel)
    assert StudioRunner._select_seo_source_files(str(tmp_path)) == []


def test_select_seo_files_dedupes_and_caps_at_six(tmp_path):
    for i in range(10):
        _touch(tmp_path, f"page{i}.html")
    picks = StudioRunner._select_seo_source_files(str(tmp_path))
    assert len(picks) == 6
    assert len(set(picks)) == 6


# ── FIX 2: validate_source .html branch ──────────────────────────────────────
def test_validate_html_requires_document_root():
    ok, err = validate_source("index.html", "<div>just a fragment</div>")
    assert ok is False and "html" in err.lower()


def test_validate_html_rejects_truncated_rewrite():
    ok, err = validate_source(
        "index.html", "<!doctype html><html><head><title>x</title></head><body>")
    assert ok is False  # no closing </html> — a token-truncated rewrite


def test_validate_complete_html_passes():
    ok, _ = validate_source(
        "index.html",
        '<!DOCTYPE html><html lang="en"><head><title>x</title></head>'
        "<body></body></html>")
    assert ok is True


def test_validate_html_accepts_html_tag_without_doctype():
    ok, _ = validate_source("page.htm", "<html><head></head><body></body></html>")
    assert ok is True


# ── FIX 3: code_improver preserves <script src> entrypoints ──────────────────
def test_html_rewrite_dropping_script_entrypoint_rejected():
    orig = ('<!doctype html><html><body><div id="root"></div>'
            '<script src="/src/main.js"></script></body></html>')
    dropped = "<!doctype html><html><body><h1>hi</h1></body></html>"
    assert CodeImproverAgent._preserves_html_entrypoints("index.html", orig, dropped) is False


def test_html_rewrite_keeping_entrypoints_accepted():
    orig = '<html><body><script src="/src/main.js"></script></body></html>'
    kept = ('<html lang="en"><head><title>t</title></head><body><h1>hi</h1>'
            '<script src="/src/main.js"></script></body></html>')
    assert CodeImproverAgent._preserves_html_entrypoints("index.html", orig, kept) is True


def test_html_inline_script_only_original_unaffected():
    orig = "<html><body><script>console.log(1)</script></body></html>"  # no src
    new = "<html><body><h1>hi</h1></body></html>"
    assert CodeImproverAgent._preserves_html_entrypoints("index.html", orig, new) is True


def test_non_html_target_never_entrypoint_guarded():
    assert CodeImproverAgent._preserves_html_entrypoints(
        "app/page.jsx", "import x", "totally different") is True


# ── FIX 6: the runner dispatch/rollback path ─────────────────────────────────
class _FakeImprover(BaseAgent):
    """A code_improve agent whose ``mutate`` callback performs the 'repair', so a test
    controls exactly what the dispatched improver writes into the tree."""

    def __init__(self, bus, mutate):
        super().__init__("improver", "code_improve", "stub", bus)
        self.add_capability(AgentCapability("code_improve"))
        self._mutate = mutate
        self.calls = 0

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def execute(self, task: TaskRequest) -> TaskResult:
        self.calls += 1
        self._mutate(task.payload)
        return TaskResult(task_id=task.task_id, success=True, output={})


class _Proof:
    def __init__(self, passed):
        self.passed = passed


def _seo_fixture(tmp_path, html, stack="static"):
    runner = _runner()
    plan = Planner(Settings()).plan("a landing page", "site")
    plan.stack = stack
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "index.html").write_text(html, encoding="utf-8")
    manifest = BuildManifest(slug="site", brief="a landing page")
    return runner, plan, proj, manifest


async def test_seo_no_dispatch_without_code_improve_capability(tmp_path, monkeypatch):
    # (i) hard issues but no code_improve agent -> record, never dispatch, verdict untouched.
    runner, plan, proj, manifest = _seo_fixture(tmp_path, BARE_HTML)
    called = {"proof": 0}
    monkeypatch.setattr(
        runner_mod, "proof_run",
        lambda *a, **k: called.__setitem__("proof", called["proof"] + 1) or _Proof(True))
    await runner._run_seo_check(manifest, plan, str(proj), "cid", {})
    assert manifest.extra["seo"]["ok"] is False   # recorded advisory verdict
    assert "seo_repair" not in manifest.extra       # no repair path entered
    assert called["proof"] == 0                      # no re-proof


async def test_seo_no_dispatch_when_no_selectable_files(tmp_path, monkeypatch):
    # (ii) gaps exist but nothing selectable (a metadata-only tree) -> no doomed dispatch.
    runner = _runner()
    plan = Planner(Settings()).plan("a nextjs site", "site")
    plan.stack = "nextjs"
    proj = tmp_path / "p"
    (proj / "lib").mkdir(parents=True)
    (proj / "lib" / "seo.ts").write_text(
        'export const metadata = { title: "Only Title" };', encoding="utf-8")
    manifest = BuildManifest(slug="site", brief="a nextjs site")
    improver = _FakeImprover(runner.event_bus, lambda payload: None)
    await runner.orchestrator.register(improver)
    called = {"proof": 0}
    monkeypatch.setattr(
        runner_mod, "proof_run",
        lambda *a, **k: called.__setitem__("proof", called["proof"] + 1) or _Proof(True))
    await runner._run_seo_check(manifest, plan, str(proj), "cid", {})
    assert improver.calls == 0
    assert called["proof"] == 0
    assert "seo_repair" not in manifest.extra


async def test_seo_repair_rolled_back_when_proof_fails(tmp_path, monkeypatch):
    # (iii) dispatched repair mutates a target + creates a file, then proof FAILS ->
    # target restored byte-identical, created file deleted, verdict unchanged, kept False.
    runner, plan, proj, manifest = _seo_fixture(tmp_path, BARE_HTML)
    original = (proj / "index.html").read_bytes()

    def mutate(payload):
        root = Path(payload["project_dir"])
        (root / "index.html").write_text("<html>BROKEN REWRITE</html>", encoding="utf-8")
        (root / "extra.js").write_text("console.log('new')", encoding="utf-8")

    improver = _FakeImprover(runner.event_bus, mutate)
    await runner.orchestrator.register(improver)
    monkeypatch.setattr(runner_mod, "proof_run", lambda *a, **k: _Proof(False))

    await runner._run_seo_check(manifest, plan, str(proj), "cid", {})

    assert improver.calls == 1
    assert (proj / "index.html").read_bytes() == original   # restored byte-identical
    assert not (proj / "extra.js").exists()                  # created file deleted
    assert manifest.extra["seo_repair"]["kept"] is False


async def test_seo_repair_kept_when_proof_passes(tmp_path, monkeypatch):
    # (iv) dispatched repair fixes the page and proof PASSES -> kept, seo re-scanned,
    # manifest.files refreshed with the new file.
    runner, plan, proj, manifest = _seo_fixture(tmp_path, BARE_HTML)

    def mutate(payload):
        root = Path(payload["project_dir"])
        (root / "index.html").write_text(FULL_HTML, encoding="utf-8")
        (root / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")

    improver = _FakeImprover(runner.event_bus, mutate)
    await runner.orchestrator.register(improver)
    monkeypatch.setattr(runner_mod, "proof_run", lambda *a, **k: _Proof(True))

    await runner._run_seo_check(manifest, plan, str(proj), "cid", {})

    assert improver.calls == 1
    assert manifest.extra["seo_repair"]["kept"] is True
    assert manifest.extra["seo"]["ok"] is True               # re-scan reflects the fix
    assert (proj / "index.html").read_text() == FULL_HTML    # repair survived
    assert "robots.txt" in manifest.files                    # manifest refreshed


async def test_seo_repair_rolled_back_on_dispatch_timeout(tmp_path, monkeypatch):
    # (v) the dispatch itself raises (timeout) AFTER a partial write -> rolled back
    # unconditionally (an incomplete repair is never kept, even without a re-proof).
    runner, plan, proj, manifest = _seo_fixture(tmp_path, BARE_HTML)
    original = (proj / "index.html").read_bytes()
    improver = _FakeImprover(runner.event_bus, lambda p: None)  # only to pass the cap gate
    await runner.orchestrator.register(improver)

    async def boom(task):
        (proj / "index.html").write_text("<html>PARTIAL", encoding="utf-8")  # mid-write
        raise TimeoutError("dispatch timed out")

    monkeypatch.setattr(runner.orchestrator, "submit", boom)
    monkeypatch.setattr(
        runner_mod, "proof_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("proof must not run on a failed dispatch")))

    await runner._run_seo_check(manifest, plan, str(proj), "cid", {})

    assert (proj / "index.html").read_bytes() == original    # partial write rolled back
    assert manifest.extra["seo_repair"]["kept"] is False
