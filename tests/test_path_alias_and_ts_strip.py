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


# ---- do-no-harm regressions (audit-confirmed) -----------------------------
def test_alias_merges_jsonc_tsconfig_without_clobbering(tmp_path):
    # tsconfig with comments + trailing comma must be MERGED, not reset (jsx/strict kept).
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text("import x from '@/lib/x';")
    (tmp_path / "tsconfig.json").write_text(
        '{\n  // base\n  "compilerOptions": {\n    "jsx": "preserve",\n    "strict": true,\n  },\n}\n')
    assert ensure_path_alias_config(tmp_path) == ["tsconfig.json"]
    cfg = json.loads((tmp_path / "tsconfig.json").read_text())
    assert cfg["compilerOptions"]["jsx"] == "preserve"      # preserved, not clobbered
    assert cfg["compilerOptions"]["strict"] is True
    assert cfg["compilerOptions"]["paths"]["@/*"] == ["./*"]


def test_alias_refuses_to_clobber_unparseable_config(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text("import x from '@/lib/x';")
    broken = '{ "compilerOptions": { this is not json at all }'
    (tmp_path / "tsconfig.json").write_text(broken)
    assert ensure_path_alias_config(tmp_path) == []          # do-no-harm: no write
    assert (tmp_path / "tsconfig.json").read_text() == broken  # untouched


def test_alias_maps_src_layout_to_src(tmp_path):
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "page.jsx").write_text("import x from '@/lib/x';")
    assert ensure_path_alias_config(tmp_path) == ["jsconfig.json"]
    cfg = json.loads((tmp_path / "jsconfig.json").read_text())
    assert cfg["compilerOptions"]["paths"]["@/*"] == ["./src/*"]


def test_strip_is_conservative(tmp_path):
    f = tmp_path / "x.js"
    f.write_text(
        "import type { A } from './a';\n"          # drop
        "export type T = z.infer<typeof s>;\n"      # drop (self-contained)
        "export type Big = {\n  a: number;\n};\n"   # KEEP (multi-line, would corrupt)
        "/*\n export type C = string;\n */\n"        # KEEP (inside block comment)
        "export const real = 1;\n")                  # KEEP
    strip_ts_type_in_js(tmp_path)
    body = f.read_text()
    assert "import type" not in body
    assert "export type T" not in body
    assert "export type Big" in body                # multi-line left for fix-loop
    assert "export type C" in body                  # comment untouched
    assert "export const real = 1;" in body
