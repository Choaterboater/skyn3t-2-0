"""Supabase scaffold variant for the Next.js stack.

Supabase is an app-shape variant of Next.js, not a new stack vocabulary entry.
These tests pin the phrase trigger, non-theft behavior, generated env wiring,
and the standard Next.js structural proof.
"""

from __future__ import annotations

import json

from skyn3t.agents._scaffold import _implies_supabase, scaffold_for
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import proof_run


def test_supabase_trigger_matches_precise_phrases():
    for brief in (
        "a Next.js app with Supabase auth",
        "Supabase database dashboard",
        "Supabase backend for a member portal",
        "Supabase login for customers",
    ):
        assert _implies_supabase(brief), brief


def test_supabase_trigger_ignores_generic_database_auth():
    for brief in (
        "a database-backed Next.js app",
        "an auth dashboard",
        "a Firebase auth app",
        "a pricing page about Supabase",
    ):
        assert not _implies_supabase(brief), brief


def test_supabase_nextjs_brief_gets_variant_scaffold():
    files = scaffold_for("nextjs", "member-portal", "a Next.js app with Supabase auth")
    pkg = json.loads(files["package.json"])
    assert "@supabase/supabase-js" in pkg["dependencies"]
    assert "lib/supabaseClient.js" in files
    assert "NEXT_PUBLIC_SUPABASE_URL" in files["lib/supabaseClient.js"]
    assert "NEXT_PUBLIC_SUPABASE_ANON_KEY" in files["lib/supabaseClient.js"]
    assert "SUPABASE_SERVICE_ROLE_KEY" not in files["app/page.jsx"]
    assert "Supabase" in files["app/page.jsx"]


def test_supabase_readme_matches_safe_missing_config_behavior():
    files = scaffold_for("nextjs", "member-portal", "a Supabase auth dashboard")
    readme = files["README.md"]
    assert "fails fast" not in readme
    assert "missing-config" in readme
    assert "NEXT_PUBLIC_SUPABASE_URL" in readme
    assert "NEXT_PUBLIC_SUPABASE_ANON_KEY" in readme


def test_plain_nextjs_brief_is_unchanged():
    files = scaffold_for("nextjs", "blog", "a Next.js blog")
    assert "lib/supabaseClient.js" not in files
    assert "@supabase/supabase-js" not in files["package.json"]


def test_supabase_variant_passes_nextjs_structural_proof(tmp_path):
    files = scaffold_for("nextjs", "member-portal", "a Supabase auth dashboard")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(tmp_path, checklist=file_checklist("nextjs"), stack="nextjs")
    assert res.passed, res.to_dict()
