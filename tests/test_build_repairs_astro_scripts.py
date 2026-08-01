"""Deterministic build repairs: astro estree-walker pin + dangling script drop.

Both come from live golden-bench failures: an astro build dying on Node 24's
CJS named-export analysis (nested estree-walker@2), and a static site whose
package.json wired `build: node scripts/validate.mjs` to a file the model
never wrote (MODULE_NOT_FOUND killed a complete page).
"""

from __future__ import annotations

import json

from skyn3t.studio.proof_run import (
    apply_deterministic_repairs,
    drop_dangling_node_script_files,
    pin_astro_estree_walker_override,
)


def _write_pkg(tmp_path, pkg: dict) -> None:
    (tmp_path / "package.json").write_text(json.dumps(pkg, indent=2), encoding="utf-8")


def test_astro_project_gets_estree_walker_override(tmp_path):
    _write_pkg(tmp_path, {"name": "x", "dependencies": {"astro": "^4.10.0"}})

    assert pin_astro_estree_walker_override(tmp_path) == ["package.json"]
    pkg = json.loads((tmp_path / "package.json").read_text())
    assert pkg["overrides"]["estree-walker"] == "^3.0.3"
    # idempotent
    assert pin_astro_estree_walker_override(tmp_path) == []


def test_override_merges_and_skips_non_astro(tmp_path):
    _write_pkg(tmp_path, {
        "name": "x",
        "dependencies": {"astro": "^5.13.0"},
        "overrides": {"vite": "^6.0.0"},
    })
    pin_astro_estree_walker_override(tmp_path)
    pkg = json.loads((tmp_path / "package.json").read_text())
    assert pkg["overrides"] == {"vite": "^6.0.0", "estree-walker": "^3.0.3"}

    _write_pkg(tmp_path, {"name": "x", "dependencies": {"react": "^19"}})
    assert pin_astro_estree_walker_override(tmp_path) == []
    pkg = json.loads((tmp_path / "package.json").read_text())
    assert "overrides" not in pkg


def test_dangling_node_script_dropped_existing_kept(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "real.mjs").write_text("console.log('ok')", encoding="utf-8")
    _write_pkg(tmp_path, {
        "name": "x",
        "scripts": {
            "build": "node scripts/validate.mjs",      # dangling -> dropped
            "check": "node ./scripts/real.mjs",        # exists -> kept
            "dev": "vite",                             # not a node file -> kept
            "test": "node --test tests/ && node scripts/validate.mjs",  # chain -> kept
        },
    })

    removed = drop_dangling_node_script_files(tmp_path)

    assert removed == ["scripts.build"]
    scripts = json.loads((tmp_path / "package.json").read_text())["scripts"]
    assert set(scripts) == {"check", "dev", "test"}
    # idempotent
    assert drop_dangling_node_script_files(tmp_path) == []


def test_apply_deterministic_repairs_wires_both(tmp_path):
    _write_pkg(tmp_path, {
        "name": "x",
        "dependencies": {"astro": "^5.13.0"},
        "scripts": {"build": "node scripts/missing.mjs"},
    })

    out = apply_deterministic_repairs(tmp_path, stack="astro")

    assert out["astro_estree_pin"] == ["package.json"]
    assert out["dangling_scripts_dropped"] == ["scripts.build"]
    pkg = json.loads((tmp_path / "package.json").read_text())
    assert pkg["overrides"]["estree-walker"] == "^3.0.3"
    assert pkg["scripts"] == {}
