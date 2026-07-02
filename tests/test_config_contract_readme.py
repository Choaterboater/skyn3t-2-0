"""Typed fail-loud config contract in scaffold READMEs (wave-2 item 51).

Every delivered app should document that required env vars are read once at
startup and that a missing one fails fast with a single clear error — never a
silent fallback to broken behaviour. The contract is documented in the shared
README composer so every stack inherits it, and it must not regress the existing
comprehensive-README doc contract.
"""

from __future__ import annotations

from skyn3t.agents._common import KNOWN_STACKS
from skyn3t.agents._scaffold import scaffold_for

_BRIEF = "A task manager that tracks todos with due dates and priorities"


def test_every_scaffold_readme_documents_the_config_contract() -> None:
    for stack in KNOWN_STACKS:
        readme = scaffold_for(stack, "demo-app", _BRIEF).get("README.md", "")
        low = readme.lower()
        assert "## configuration" in low, f"{stack} README has no Configuration section"
        assert "startup" in low, f"{stack} README config section omits startup-read"
        # fail-loud, not a silent fallback
        assert "missing" in low and "error" in low, (
            f"{stack} README config section omits the fail-loud contract"
        )


def test_config_contract_has_no_todo_placeholder() -> None:
    # The doc-quality contract forbids a literal TODO in the README.
    readme = scaffold_for("react_vite", "demo-app", _BRIEF).get("README.md", "")
    assert "todo" not in readme.lower().replace("todos", "")
