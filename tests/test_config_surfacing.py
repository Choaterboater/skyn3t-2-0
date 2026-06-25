"""Tests for config surfacing: ConfigSpec, detection (brief + code), settings-UI
generation, static wiring verification, and the integration-verifier hook."""
from __future__ import annotations

from pathlib import Path

from skyn3t.agents import integration_verifier as iv
from skyn3t.agents.config_detector import (
    detect,
    detect_from_brief,
    detect_from_code,
)
from skyn3t.agents.config_ui_agent import (
    apply_config,
    check_config_wiring,
    generate_config_ui,
)
from skyn3t.agents.validate import validate_source
from skyn3t.studio.config_spec import ConfigKey, ConfigSpec, kind_for, scope_for


# ---- ConfigSpec ----------------------------------------------------------

def test_config_spec_roundtrip_and_queries():
    spec = ConfigSpec(
        keys=[ConfigKey("VITE_API_KEY", kind="api_key", scope="client"),
              ConfigKey("DATABASE_URL", kind="url", scope="server")],
        apis=["Weather API"],
    )
    again = ConfigSpec.from_dict(spec.to_dict())
    assert again.key_names() == ["VITE_API_KEY", "DATABASE_URL"]
    assert [k.name for k in again.client_keys()] == ["VITE_API_KEY"]
    assert [k.name for k in again.server_keys()] == ["DATABASE_URL"]
    assert not again.is_empty()
    assert ConfigSpec().is_empty()


def test_config_spec_merge_unions_by_name():
    a = ConfigSpec(keys=[ConfigKey("API_KEY", description="from brief")], apis=["X"])
    b = ConfigSpec(keys=[ConfigKey("API_KEY", description="from code"),
                         ConfigKey("DB_URL", kind="url")], apis=["X", "Y"])
    merged = a.merge(b)
    assert sorted(merged.key_names()) == ["API_KEY", "DB_URL"]
    # self wins on conflict -> keeps the brief's richer description
    api = next(k for k in merged.keys if k.name == "API_KEY")
    assert api.description == "from brief"
    assert merged.apis == ["X", "Y"]


def test_kind_and_scope_inference():
    assert kind_for("OPENAI_API_KEY") == "api_key"
    assert kind_for("DATABASE_URL") == "url"
    assert kind_for("SESSION_SECRET") == "secret"
    assert kind_for("ENABLE_DARK_MODE") == "toggle"
    assert kind_for("APP_TITLE") == "value"
    assert scope_for("VITE_FOO") == "client"
    assert scope_for("SECRET_FOO") == "server"


# ---- detect_from_brief (keyword fallback, llm_fn=None) -------------------

def test_brief_weather_api_key_client_for_web_stack():
    spec = detect_from_brief("a weather dashboard that needs a weather API key",
                             "react", llm_fn=None)
    keys = spec.client_keys()
    assert keys, "expected a client-scoped key for a web stack"
    k = keys[0]
    assert k.kind == "api_key"
    assert k.scope == "client"
    assert k.name.startswith("VITE_")  # client keys get a browser-visible prefix


def test_brief_api_key_server_for_backend_stack():
    spec = detect_from_brief("a weather service exposing a weather API key",
                             "fastapi", llm_fn=None)
    assert spec.server_keys()
    assert not spec.client_keys()


def test_brief_database_and_auth_stay_server_even_on_web():
    spec = detect_from_brief("a react app with login and a postgres database",
                             "react", llm_fn=None)
    names = spec.key_names()
    assert "DATABASE_URL" in names
    assert "AUTH_SECRET" in names
    for k in spec.keys:
        if k.name in ("DATABASE_URL", "AUTH_SECRET"):
            assert k.scope == "server"


def test_brief_with_no_config_need_is_empty():
    spec = detect_from_brief("a simple offline counter app", "react", llm_fn=None)
    assert spec.is_empty()


def test_brief_llm_fn_used_when_provided():
    raw = ('{"keys": [{"name": "VITE_FANCY", "kind": "value", "scope": "client"}], '
           '"apis": ["Fancy"]}')
    spec = detect_from_brief("anything", "react", llm_fn=lambda _p: raw)
    assert spec.key_names() == ["VITE_FANCY"]


def test_brief_llm_fn_error_falls_back_to_keywords():
    def boom(_p):
        raise RuntimeError("model down")
    spec = detect_from_brief("needs a weather API key", "react", llm_fn=boom)
    assert spec.key_names()  # keyword fallback still produced something


# ---- detect_from_code (EnvScanner wrap) ----------------------------------

def test_detect_from_code_splits_client_and_server(tmp_path: Path):
    (tmp_path / "app.js").write_text(
        "const a = import.meta.env.VITE_FOO;\nconst b = process.env.SECRET_BAR;\n")
    (tmp_path / "svc.py").write_text("import os\nx = os.getenv('PLAIN_BAZ')\n")
    spec = detect_from_code(tmp_path)
    names = spec.key_names()
    assert "VITE_FOO" in names and "SECRET_BAR" in names and "PLAIN_BAZ" in names
    by = {k.name: k for k in spec.keys}
    assert by["VITE_FOO"].scope == "client"
    assert by["SECRET_BAR"].scope == "server"
    assert by["SECRET_BAR"].kind == "secret"


# ---- settings UI generation ---------------------------------------------

def _client_spec():
    return ConfigSpec(keys=[ConfigKey("VITE_API_KEY", kind="api_key", scope="client",
                                      description="Your API key")])


def test_generate_react_settings_ui_valid(tmp_path: Path):
    files = generate_config_ui(tmp_path, "react", _client_spec())
    assert set(files) == {"src/config.js", "src/Settings.jsx"}
    for rel in files:
        content = (tmp_path / rel).read_text()
        ok, err = validate_source(rel, content)
        assert ok, f"{rel} failed validation: {err}"
    # the accessor embeds the key so the form renders it
    assert "VITE_API_KEY" in (tmp_path / "src/config.js").read_text()


def test_apply_config_emits_env_example(tmp_path: Path):
    """apply_config writes a .env.example listing detected keys, so an app that
    reads env vars directly is configurable without the accessor (finding #13)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export default function App(){return null}\n")
    summary = apply_config(tmp_path, "a weather dashboard needing a weather API key", "react")
    assert ".env.example" in summary["files_written"]
    body = (tmp_path / ".env.example").read_text()
    spec = ConfigSpec.from_dict(summary["config_spec"])
    assert spec.keys, "expected at least one detected key"
    for k in spec.keys:
        assert k.name in body
    # idempotent: a second run does not overwrite / re-list it
    again = apply_config(tmp_path, "a weather dashboard needing a weather API key", "react")
    assert ".env.example" not in again["files_written"]


def test_nextjs_settings_is_a_real_route(tmp_path: Path):
    """Next.js is file-routed: the settings UI must be an app/ route, not an
    orphaned src/Settings.jsx nothing can reach (finding #14)."""
    files = generate_config_ui(tmp_path, "nextjs", _client_spec())
    assert "app/settings/page.jsx" in files
    assert "src/Settings.jsx" not in files
    assert "app/config.js" in files
    page = (tmp_path / "app/settings/page.jsx").read_text()
    assert page.startswith("'use client'")          # uses useState -> client component
    assert "export default" in page
    assert "from '../config.js'" in page              # accessor import resolves
    ok, err = validate_source("app/settings/page.jsx", page)
    assert ok, err
    # wiring detection recognizes the routed settings page
    wiring = check_config_wiring(tmp_path, _client_spec())
    assert wiring["settings_ui_present"] and wiring["accessor_present"]


def test_wiring_reports_accessor_imported_advisory(tmp_path: Path):
    """check_config_wiring surfaces whether real app code imports the accessor
    (finding #24) — advisory only, never a gap that fails a build."""
    generate_config_ui(tmp_path, "react", _client_spec())  # config.js + Settings.jsx only
    w1 = check_config_wiring(tmp_path, _client_spec())
    assert w1["accessor_imported"] is False  # only Settings imports it
    assert w1["accessor_imported"] not in (g for g in w1["gaps"])  # not a gap
    # now real app code imports it
    (tmp_path / "src" / "App.jsx").write_text(
        "import { getConfig } from './config.js'\nexport default function App(){return null}\n")
    w2 = check_config_wiring(tmp_path, _client_spec())
    assert w2["accessor_imported"] is True


def test_generate_static_settings_ui(tmp_path: Path):
    files = generate_config_ui(tmp_path, "static", _client_spec())
    assert set(files) == {"config.js", "settings.html"}
    ok, err = validate_source("config.js", (tmp_path / "config.js").read_text())
    assert ok, err


def test_no_ui_when_no_client_keys(tmp_path: Path):
    server_only = ConfigSpec(keys=[ConfigKey("DATABASE_URL", kind="url", scope="server")])
    assert generate_config_ui(tmp_path, "react", server_only) == []


def test_no_ui_for_non_web_stack(tmp_path: Path):
    assert generate_config_ui(tmp_path, "python", _client_spec()) == []


def test_generate_does_not_overwrite_by_default(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/config.js").write_text("// mine\n")
    files = generate_config_ui(tmp_path, "react", _client_spec())
    assert "src/config.js" not in files  # preserved
    assert (tmp_path / "src/config.js").read_text() == "// mine\n"


# ---- wiring verification -------------------------------------------------

def test_check_wiring_flags_missing_ui_and_hardcoded_url(tmp_path: Path):
    (tmp_path / "app.js").write_text(
        'fetch("https://api.weather.example/v1/now?key=abc");\n')
    wiring = check_config_wiring(tmp_path, _client_spec())
    assert not wiring["ok"]
    assert any("settings UI" in g for g in wiring["gaps"])
    assert any("api.weather.example" in u for u in wiring["hardcoded_urls"])


def test_check_wiring_ignores_localhost(tmp_path: Path):
    (tmp_path / "app.js").write_text('fetch("http://localhost:8000/api");\n')
    wiring = check_config_wiring(tmp_path, ConfigSpec())
    assert wiring["hardcoded_urls"] == []


def test_check_wiring_ok_when_ui_present(tmp_path: Path):
    generate_config_ui(tmp_path, "react", _client_spec())
    wiring = check_config_wiring(tmp_path, _client_spec())
    assert wiring["accessor_present"] and wiring["settings_ui_present"]


# ---- apply_config orchestrator ------------------------------------------

def test_apply_config_end_to_end(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/App.jsx").write_text(
        'export default function App(){ return null }\n'
        '// fetch("https://api.weather.example/v1")\n')
    summary = apply_config(tmp_path, "a weather dashboard with a weather API key", "react")
    assert summary["needs_config"]
    # a client key was detected -> a settings UI was generated
    assert "src/Settings.jsx" in summary["files_written"]
    spec = ConfigSpec.from_dict(summary["config_spec"])
    assert spec.client_keys()


# ---- integration_verifier hook ------------------------------------------

def test_integration_verifier_finds_hardcoded_url(tmp_path: Path):
    (tmp_path / "main.js").write_text('const u = "https://api.thirdparty.io/data";\n')
    urls = iv.find_hardcoded_urls(tmp_path)
    assert "https://api.thirdparty.io/data" in urls
    wiring = iv.analyze(tmp_path)
    assert "https://api.thirdparty.io/data" in wiring["hardcoded_urls"]


def test_integration_verifier_skips_config_files(tmp_path: Path):
    (tmp_path / "config.js").write_text('const d = "https://api.default.io";\n')
    assert iv.find_hardcoded_urls(tmp_path) == []
