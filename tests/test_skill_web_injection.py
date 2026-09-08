from __future__ import annotations

from skyn3t.intelligence.skill_library import SkillLibrary
from skyn3t.studio.runner import _WEB_STACKS, _web_design_tags


def _lib():
    lib = SkillLibrary()
    lib.add("Frontend UI Engineering", "Use semantic HTML, a11y, responsive layout.",
            stack="generic", tags=["frontend", "design", "ui", "web"], slug="frontend-ui-engineering")
    lib.add("Python CLI shape", "argparse + entrypoint.", stack="python", tags=["cli"], slug="py-cli")
    return lib


def test_web_stack_surfaces_design_skill_first():
    lib = _lib()
    tags = _web_design_tags("react")
    top = lib.relevant("react", tags=tags, limit=2)
    assert "frontend-ui-engineering" in [s.slug for s in top]


def test_non_web_stack_does_not_force_design_tags():
    assert _web_design_tags("python") is None
    assert "react" in _WEB_STACKS and "fastapi" in _WEB_STACKS and "python" not in _WEB_STACKS
    # fastapi is intentionally a web stack (serves UIs / has interface-design concerns)
    assert _web_design_tags("fastapi") is not None


def test_semantic_selection_preserves_complete_procedure_and_advisory_label(
    tmp_path, monkeypatch,
):
    from skyn3t.config.settings import Settings
    from skyn3t.core.events import EventBus
    from skyn3t.core.orchestrator import Orchestrator
    from skyn3t.studio.runner import StudioRunner

    body = (
        "Browser proof with isolated test state. " * 20
        + "\nDone only after restart succeeds; report missing native proof explicitly."
    )
    lib = SkillLibrary(tmp_path / "skills")
    skill = lib.add("Browser proof", body, stack="react", slug="browser-proof")
    monkeypatch.setattr(lib, "relevant", lambda *args, **kwargs: [])
    bus = EventBus()
    runner = StudioRunner(
        bus,
        Orchestrator(bus),
        settings=Settings(llm_backend="stub", data_dir=tmp_path),
        skills=lib,
    )

    advice, slugs = runner._skill_advice("react", "browser proof isolated test state")

    assert slugs == [skill.slug]
    assert body in advice
    assert "advisory" in advice
