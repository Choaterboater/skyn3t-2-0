"""reconcile_tauri_cargo_features — swap invalid/hallucinated Tauri Cargo feature names
(e.g. fs-read-text-file) for a known-good superset so the Rust shell builds. The
tauri proof only builds the frontend, so this is the net that keeps the shell valid."""
from __future__ import annotations

from skyn3t.studio.proof_run import reconcile_tauri_cargo_features

_GOOD = '[package]\nname = "x"\n\n[dependencies]\ntauri = { version = "1", features = ["dialog-all", "fs-all", "shell-open"] }\n'
_BAD = '[package]\nname = "x"\n\n[dependencies]\ntauri = { version = "1", features = ["dialog-open", "fs-read-text-file", "fs-write-text-file", "shell-open"] }\n'


def _cargo(tmp_path, body):
    (tmp_path / "src-tauri").mkdir()
    (tmp_path / "src-tauri" / "Cargo.toml").write_text(body)


def test_fixes_hallucinated_features(tmp_path):
    import json
    _cargo(tmp_path, _BAD)
    (tmp_path / "src-tauri" / "tauri.conf.json").write_text(
        json.dumps({"tauri": {"allowlist": {"fs": {"readTextFile": True}}}}))
    out_files = reconcile_tauri_cargo_features(tmp_path)
    assert "src-tauri/Cargo.toml" in out_files
    cargo = (tmp_path / "src-tauri" / "Cargo.toml").read_text()
    assert "fs-read-text-file" not in cargo
    assert "api-all" in cargo                     # matched permissive pair
    assert 'name = "x"' in cargo                  # rest preserved
    # allowlist realigned to {all:true} so Tauri's feature<->allowlist check passes
    conf = json.loads((tmp_path / "src-tauri" / "tauri.conf.json").read_text())
    assert conf["tauri"]["allowlist"] == {"all": True}


def test_noop_on_valid_features(tmp_path):
    _cargo(tmp_path, _GOOD)
    assert reconcile_tauri_cargo_features(tmp_path) == []
    assert (tmp_path / "src-tauri" / "Cargo.toml").read_text() == _GOOD


def test_noop_without_src_tauri(tmp_path):
    assert reconcile_tauri_cargo_features(tmp_path) == []
