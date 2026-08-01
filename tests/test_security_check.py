from __future__ import annotations

from types import SimpleNamespace

from skyn3t.config.settings import Settings
from skyn3t.core.events import EventBus
from skyn3t.core.orchestrator import Orchestrator
from skyn3t.studio.manifest import BuildManifest
from skyn3t.studio.runner import StudioRunner
from skyn3t.studio.security_check import check_security, rewrite_secret_literals


def test_security_check_flags_bundled_secret(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.js").write_text(
        "export const apiKey = 'sk-live-1234567890abcdef';\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "react")

    assert verdict["ok"] is False
    assert any("secret" in issue.lower() for issue in verdict["issues"])


def test_security_check_covers_web_stack_alias_component_files(tmp_path):
    (tmp_path / "App.vue").write_text(
        "<script>const apiKey = 'sk-live-1234567890abcdef';</script>\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "vuejs")

    assert verdict["skipped"] is False
    assert verdict["ok"] is False
    assert any("App.vue" in issue for issue in verdict["issues"])


def test_security_check_covers_vite_and_react_native_sources(tmp_path):
    (tmp_path / "App.tsx").write_text(
        "export const apiKey = 'sk-live-1234567890abcdef';\n",
        encoding="utf-8",
    )

    for stack in ("vite", "react_native"):
        verdict = check_security(tmp_path, stack)
        assert verdict["skipped"] is False
        assert verdict["ok"] is False


def test_security_check_skips_artifact_stack(tmp_path):
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")

    verdict = check_security(tmp_path, "python_cli")

    assert verdict["skipped"] is True


def test_security_check_does_not_treat_query_selector_as_sql(tmp_path):
    (tmp_path / "main.js").write_text(
        'const option = root.querySelector(`option[value="${CSS.escape(value)}"]`);\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "static")

    assert verdict["ok"] is True
    assert verdict["issues"] == []


def test_security_check_does_not_treat_react_delete_label_as_sql(tmp_path):
    (tmp_path / "HabitCard.jsx").write_text(
        "const label = `Delete ${habit.title}`;\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "react")

    assert verdict["ok"] is True
    assert verdict["issues"] == []


def test_security_check_still_flags_real_sql_interpolation(tmp_path):
    (tmp_path / "server.js").write_text(
        "const sql = `SELECT * FROM users WHERE id = ${userId}`;\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "express")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_python_fstring_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = f"SELECT * FROM users WHERE id = {uid}"\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["skipped"] is False
    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_ignores_fstring_without_sql(tmp_path):
    (tmp_path / "ui.py").write_text(
        'label = f"Delete {habit.title} permanently?"\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["issues"] == []


def test_security_check_flags_triple_quoted_fstring_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = f"""SELECT * FROM users WHERE id = {uid}"""\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_raw_fstring_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = fr"DELETE FROM sessions WHERE token = {tok}"\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_triple_quoted_percent_format_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = """SELECT * FROM users WHERE id = %s""" % uid\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_ignores_triple_quoted_fstring_prose(tmp_path):
    (tmp_path / "ui.py").write_text(
        'label = f"""Select your {item} from the menu"""\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["issues"] == []


def test_security_check_flags_multiline_js_template_sql(tmp_path):
    (tmp_path / "db.js").write_text(
        "const q = `\n  SELECT *\n  FROM users\n  WHERE id = ${uid}\n`;\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "express")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_multiline_python_fstring_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = f"""\n    SELECT *\n    FROM users\n    WHERE id = {uid}\n"""\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_multiline_percent_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        "query = '''\nSELECT *\nFROM users\nWHERE id = %s\n''' % uid\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_ignores_multiline_prose_template(tmp_path):
    (tmp_path / "Help.jsx").write_text(
        "const help = `Select your plan\nfrom the options below ${plan}`;\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "react")

    assert verdict["issues"] == []


def test_security_check_does_not_chain_adjacent_literals(tmp_path):
    (tmp_path / "help.js").write_text(
        "const a = `Use SELECT * FROM users to list`;\nconst b = `${y}`;\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "static")

    assert verdict["issues"] == []


def test_security_check_ignores_lowercase_multiline_docstring(tmp_path):
    (tmp_path / "notes.py").write_text(
        '"""\nRun select * from users to see everyone.\nThen call {helper}\n"""\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["issues"] == []


def test_security_gate_downgrades_critical_findings(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.js").write_text("eval(req.query.code)\n", encoding="utf-8")
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"),
        memory=None,
    )
    man = BuildManifest(slug="x", brief="api", stack="express")

    score, verdict = runner._run_security_gate(
        man, str(tmp_path), SimpleNamespace(stack="express"), 91.0, "go"
    )

    assert verdict == "no_go"
    assert score == 49.0
    assert man.extra["security_check"]["ok"] is False


def test_security_check_flags_percent_d_format_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = "SELECT * FROM users WHERE id = %d" % uid\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_named_percent_format_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = "SELECT * FROM users WHERE id = %(uid)s"\n'
        'cursor.execute(query, {"uid": uid})\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_format_map_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = "SELECT * FROM users WHERE id = {uid}".format_map(locals())\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_js_concat_sql(tmp_path):
    (tmp_path / "db.js").write_text(
        'const q = "SELECT * FROM users WHERE id = ".concat(uid);\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "express")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_multiline_percent_d_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = """\nSELECT *\nFROM users\nWHERE id = %d\n""" % uid\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_ignores_percent_sign_prose(tmp_path):
    (tmp_path / "ui.py").write_text(
        'label = f"Select your {item} from the menu — 50% off today"\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["issues"] == []


def test_security_check_header_warning_covers_agent_stack_spellings(tmp_path):
    (tmp_path / "server.js").write_text(
        "const express = require('express');\nconst app = express();\n",
        encoding="utf-8",
    )

    for stack in ("express", "node_express", "nextjs", "next", "fastapi", "rag", "workflow"):
        verdict = check_security(tmp_path, stack)
        assert verdict["skipped"] is False
        assert any("security-header" in w for w in verdict["warnings"]), stack


def test_security_check_header_warning_set_covers_every_api_spelling():
    from skyn3t.studio import security_check

    assert {
        "express", "node_express", "nextjs", "next", "fastapi", "rag", "workflow",
    } <= security_check._HEADER_WARN_STACKS


def test_security_check_header_warning_quiet_for_ui_stacks(tmp_path):
    (tmp_path / "App.jsx").write_text(
        "export default function App() { return <div/>; }\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "react")

    assert verdict["warnings"] == []


def test_security_check_header_warning_quiet_when_headers_present(tmp_path):
    (tmp_path / "server.js").write_text(
        "app.use((req, res, next) => {\n"
        "  res.set('Content-Security-Policy', \"default-src 'self'\");\n"
        "  next();\n"
        "});\n",
        encoding="utf-8",
    )

    for stack in ("node_express", "next"):
        verdict = check_security(tmp_path, stack)
        assert verdict["warnings"] == [], stack


def test_security_check_flags_percent_r_format_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = "SELECT * FROM users WHERE name = %r" % name\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_multiline_percent_r_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = """\nSELECT *\nFROM users\nWHERE name = %r\n""" % name\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_replace_into_percent_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = "REPLACE INTO users (id, name) VALUES (%s, \'%s\')" % (uid, name)\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_replace_into_template_sql(tmp_path):
    (tmp_path / "db.js").write_text(
        "const q = `REPLACE INTO sessions (token, uid) VALUES ('${tok}', ${uid})`;\n",
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "express")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_python_join_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = " ".join(["SELECT * FROM users WHERE id =", str(uid)])\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_multiline_join_sql(tmp_path):
    (tmp_path / "db.py").write_text(
        'query = "\n".join([\n    "SELECT *",\n    "FROM users",\n    "WHERE id =",\n    str(uid),\n])\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_flags_js_join_sql(tmp_path):
    (tmp_path / "db.js").write_text(
        'const q = ["SELECT * FROM users WHERE id =", uid].join(" ");\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "express")

    assert verdict["ok"] is False
    assert any("SQL built" in issue for issue in verdict["issues"])


def test_security_check_ignores_join_prose(tmp_path):
    (tmp_path / "ui.py").write_text(
        'label = " ".join(["Select your plan", "from the menu", name])\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["issues"] == []


def test_security_check_ignores_join_of_pure_literals(tmp_path):
    (tmp_path / "db.py").write_text(
        'lines = " ".join(["SELECT * FROM users", "ORDER BY name", "LIMIT 10"])\n',
        encoding="utf-8",
    )

    verdict = check_security(tmp_path, "fastapi")

    assert verdict["issues"] == []


def test_rewrite_secret_literals_rewrites_python_assignment(tmp_path):
    (tmp_path / "config.py").write_text(
        'api_key = "sk-live-1234567890abcdef"\n',
        encoding="utf-8",
    )

    report = rewrite_secret_literals(tmp_path)

    assert report["ok"] is True
    assert report["errors"] == []
    text = (tmp_path / "config.py").read_text(encoding="utf-8")
    assert 'api_key = os.getenv("API_KEY", "")' in text
    assert "sk-live-1234567890abcdef" not in text
    assert "import os" in text
    compile(text, "config.py", "exec")  # the rewrite stays valid Python
    env_example = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "API_KEY=" in env_example
    # The same gate must now pass on the rewritten tree.
    verdict = check_security(tmp_path, "fastapi")
    assert not any("secret" in issue.lower() for issue in verdict["issues"])
    assert verdict["ok"] is True


def test_rewrite_secret_literals_inserts_os_import_after_docstring(tmp_path):
    (tmp_path / "config.py").write_text(
        '"""App config."""\nfrom __future__ import annotations\n\n'
        'api_key = "sk-live-1234567890abcdef"\n',
        encoding="utf-8",
    )

    rewrite_secret_literals(tmp_path)

    text = (tmp_path / "config.py").read_text(encoding="utf-8")
    assert text.startswith(
        '"""App config."""\nfrom __future__ import annotations\nimport os\n'
    )
    compile(text, "config.py", "exec")


def test_rewrite_secret_literals_keeps_existing_os_import(tmp_path):
    (tmp_path / "config.py").write_text(
        '"""App config."""\nimport os\n\napi_key = "sk-live-1234567890abcdef"\n',
        encoding="utf-8",
    )

    rewrite_secret_literals(tmp_path)

    text = (tmp_path / "config.py").read_text(encoding="utf-8")
    assert text.count("import os") == 1
    assert text.startswith('"""App config."""\nimport os\n')


def test_rewrite_secret_literals_rewrites_js_const(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.js").write_text(
        "export const apiKey = 'sk-live-1234567890abcdef';\n",
        encoding="utf-8",
    )

    report = rewrite_secret_literals(tmp_path)

    text = (tmp_path / "src" / "config.js").read_text(encoding="utf-8")
    assert 'export const apiKey = process.env.API_KEY || "";' in text
    assert "sk-live-1234567890abcdef" not in text
    assert report["rewritten"][0]["var"] == "API_KEY"
    assert "API_KEY=" in (tmp_path / ".env.example").read_text(encoding="utf-8")
    verdict = check_security(tmp_path, "react")
    assert verdict["ok"] is True


def test_rewrite_secret_literals_derives_camel_and_generic_names(tmp_path):
    (tmp_path / "a.js").write_text(
        "const stripeSecretKey = 'sk-live-aaaaaaaaaaaaaaaa';\n"
        "const token = 'sk-live-bbbbbbbbbbbbbbbb';\n",
        encoding="utf-8",
    )

    report = rewrite_secret_literals(tmp_path)

    text = (tmp_path / "a.js").read_text(encoding="utf-8")
    assert 'process.env.STRIPE_SECRET_KEY || ""' in text
    assert 'process.env.TOKEN_1 || ""' in text
    env_example = (tmp_path / ".env.example").read_text(encoding="utf-8")
    assert "STRIPE_SECRET_KEY=" in env_example
    assert "TOKEN_1=" in env_example
    assert len(report["rewritten"]) == 2
    verdict = check_security(tmp_path, "express")
    assert not any("secret" in issue.lower() for issue in verdict["issues"])


def test_rewrite_secret_literals_skips_non_assignment_occurrences(tmp_path):
    (tmp_path / "server.js").write_text(
        "console.log('sk-live-1234567890abcdef');\n"
        "const url = `https://x.test/${'sk-live-1234567890abcdef'}`;\n"
        "const data = { 'sk-live-1234567890abcdef': true };\n",
        encoding="utf-8",
    )

    report = rewrite_secret_literals(tmp_path)

    assert report["rewritten"] == []
    assert len(report["skipped"]) == 3
    assert not (tmp_path / ".env.example").exists()
    # Untouched: the gate still (correctly) flags the file.
    verdict = check_security(tmp_path, "express")
    assert any("secret" in issue.lower() for issue in verdict["issues"])


def test_rewrite_secret_literals_reports_unsupported_file_type(tmp_path):
    (tmp_path / "App.vue").write_text(
        "<script>const apiKey = 'sk-live-1234567890abcdef';</script>\n",
        encoding="utf-8",
    )

    report = rewrite_secret_literals(tmp_path)

    assert report["rewritten"] == []
    assert any("not rewritten" in s["reason"] for s in report["skipped"])


def test_security_gate_repairs_secret_then_passes(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.js").write_text(
        "export const apiKey = 'sk-live-1234567890abcdef';\n",
        encoding="utf-8",
    )
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"),
        memory=None,
    )
    man = BuildManifest(slug="x", brief="web", stack="react")

    score, verdict = runner._run_security_gate(
        man, str(tmp_path), SimpleNamespace(stack="react"), 91.0, "go"
    )

    assert verdict == "go"
    assert score == 91.0
    assert man.extra["security_check"]["ok"] is True
    rewrite = man.extra["security_secret_rewrite"]
    assert rewrite["rewritten"][0]["var"] == "API_KEY"
    assert rewrite["env_example"] == ".env.example"
    rewritten_src = (tmp_path / "src" / "config.js").read_text(encoding="utf-8")
    assert "process.env.API_KEY" in rewritten_src


def test_security_gate_stays_red_when_secret_not_rewritable(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "server.js").write_text(
        "console.log('sk-live-1234567890abcdef');\n",
        encoding="utf-8",
    )
    runner = StudioRunner(
        EventBus(),
        Orchestrator(EventBus()),
        settings=Settings(projects_dir=tmp_path / "Projects", data_dir=tmp_path / "data", logs_dir=tmp_path / "logs"),
        memory=None,
    )
    man = BuildManifest(slug="x", brief="api", stack="express")

    score, verdict = runner._run_security_gate(
        man, str(tmp_path), SimpleNamespace(stack="express"), 91.0, "go"
    )

    assert verdict == "no_go"
    assert score == 49.0
    assert man.extra["security_secret_rewrite"]["rewritten"] == []
    assert man.extra["security_secret_rewrite"]["skipped"]
