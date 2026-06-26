"""Deterministic repairs for two common cheap-model defects: an unconfigured @/ import
alias (no jsconfig paths) and stray TypeScript statements in plain .js files."""
from __future__ import annotations

import json

from skyn3t.studio.proof_run import ensure_path_alias_config, strip_ts_type_in_js


def test_alias_config_written_when_at_imports_and_no_config(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "x.js").write_text("export const x = 1;")
    (tmp_path / "app" / "page.jsx").write_text("import { x } from '@/lib/x';\nexport default () => x;")
    assert ensure_path_alias_config(tmp_path) == ["jsconfig.json"]
    cfg = json.loads((tmp_path / "jsconfig.json").read_text())
    assert cfg["compilerOptions"]["paths"]["@/*"] == ["./*"]


def test_alias_config_added_to_existing_tsconfig(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.tsx").write_text("import x from '@/lib/x';")
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions": {"strict": true}}')
    assert ensure_path_alias_config(tmp_path) == ["tsconfig.json"]
    cfg = json.loads((tmp_path / "tsconfig.json").read_text())
    assert cfg["compilerOptions"]["paths"]["@/*"] == ["./*"]
    assert cfg["compilerOptions"]["strict"] is True  # preserved


def test_alias_config_noop_when_mapped_or_no_at_imports(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text("import x from './local';")  # no @/ imports
    assert ensure_path_alias_config(tmp_path) == []


def test_strip_ts_type_from_js(tmp_path):
    f = tmp_path / "schema.js"
    f.write_text("import { z } from 'zod';\nexport const s = z.object({});\n"
                 "export type T = z.infer<typeof s>;\nimport type { A } from './a';\n")
    assert "schema.js" in strip_ts_type_in_js(tmp_path)
    body = f.read_text()
    assert "export type" not in body and "import type" not in body
    assert "export const s" in body  # real code preserved


def test_strip_ts_type_leaves_tsx_untouched(tmp_path):
    (tmp_path / "a.tsx").write_text("export type T = string;\n")  # valid TS — keep
    assert strip_ts_type_in_js(tmp_path) == []
