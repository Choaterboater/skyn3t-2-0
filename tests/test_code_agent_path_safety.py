from __future__ import annotations

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
