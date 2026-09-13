from __future__ import annotations

import subprocess
import sys

from skyn3t.agents.code_agent import CodeAgent
from skyn3t.core.events import EventBus


def test_code_agent_rejects_ansi_contaminated_generated_paths(tmp_path):
    agent = CodeAgent(event_bus=EventBus())

    written = agent._write_files(
        tmp_path,
        {
            "\x1b[35massets/index.js": "export const accidental = true;\n",
            "src/App.jsx": "export default function App() { return null; }\n",
        },
    )

    assert written == ["src/App.jsx"]
    assert (tmp_path / "src" / "App.jsx").is_file()
    assert not (tmp_path / "35massets").exists()


def test_code_agent_prunes_malformed_direct_agentic_paths_for_src_layout(tmp_path):
    agent = CodeAgent(event_bus=EventBus())
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export default null;\n")
    baseline = agent._snapshot_regular_files(tmp_path)

    (tmp_path / "src" / "components").mkdir()
    (tmp_path / "src" / "components" / "UsefulCard.jsx").write_text("export default null;\n")
    (tmp_path / "src" / "components" / "MissingExtension").write_text("junk\n")
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "InventoryOverview.jsx").write_text("junk\n")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "PantryContext.jsx").write_text("junk\n")
    (tmp_path / "33msrc").mkdir()
    (tmp_path / "33msrc" / "sampleData").write_text("junk\n")
    (tmp_path / "package.js").write_text("junk\n")

    removed = agent._prune_untrusted_agentic_new_paths(
        tmp_path,
        baseline,
        {"src/App.jsx", "src/main.jsx"},
    )

    assert (tmp_path / "src" / "components" / "UsefulCard.jsx").is_file()
    assert not (tmp_path / "src" / "components" / "MissingExtension").exists()
    assert not (tmp_path / "components").exists()
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "33msrc").exists()
    assert not (tmp_path / "package.js").exists()
    assert set(removed) == {
        "33msrc/sampleData",
        "components/InventoryOverview.jsx",
        "package.js",
        "src/components/MissingExtension",
        "state/PantryContext.jsx",
    }


def test_python_agentic_pruning_preserves_imported_package_additions(tmp_path):
    agent = CodeAgent(event_bus=EventBus())
    package = tmp_path / "installation_validator"
    package.mkdir()
    (tmp_path / "main.py").write_text(
        "from installation_validator.config import VALUE\nprint(VALUE)\n"
    )
    (package / "__init__.py").write_text("")
    (package / "core.py").write_text("VERSION = 1\n")
    (package / "config.py").write_text("VALUE = 'working'\n")
    (package / "__main__.py").write_text("from .config import VALUE\nprint(VALUE)\n")

    removed = agent._prune_untrusted_agentic_new_paths(
        tmp_path, {}, {"main.py", "pyproject.toml", "installation_validator/core.py"},
    )

    result = subprocess.run(
        [sys.executable, str(tmp_path / "main.py")],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "working"
    assert removed == []


def test_python_agentic_pruning_preserves_launchers_and_input_fixtures(tmp_path):
    agent = CodeAgent(event_bus=EventBus())
    files = {
        "mist_validate.py": b"print('validator')\n",
        "config.json": b"{}\n",
        "inventory.csv": b"name,serial\nAP1,DEMO1\n",
        "design.esx": b"PK\x03\x04\x80\xff",
        "examples/config.json": b"{}\n",
        "examples/design.esx": b"PK\x03\x04\x80\xff",
        "tools/generate_examples.py": b"print('examples')\n",
    }
    for rel, content in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (tmp_path / "package.js").write_text("junk\n")
    (tmp_path / "33msrc").mkdir()
    (tmp_path / "33msrc" / "sampleData").write_text("junk\n")

    removed = agent._prune_untrusted_agentic_new_paths(
        tmp_path, {}, {"main.py", "pyproject.toml", "installation_validator/core.py"},
    )

    assert removed == ["33msrc/sampleData", "package.js"]
    for rel, content in files.items():
        assert (tmp_path / rel).read_bytes() == content


def test_agentic_text_roundtrip_does_not_corrupt_zip_inputs(tmp_path):
    agent = CodeAgent(event_bus=EventBus())
    worktree = tmp_path / "project"
    worktree.mkdir()
    archive = b"PK\x03\x04\x80\xff\x00fixture"
    for name in ("design.esx", "sample.zip"):
        (worktree / name).write_bytes(archive)

    text_files = agent._read_files(worktree)
    agent._write_files(worktree, text_files)

    assert text_files == {}
    for name in ("design.esx", "sample.zip"):
        assert (worktree / name).read_bytes() == archive
