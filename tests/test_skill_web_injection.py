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
