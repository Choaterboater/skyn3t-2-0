"""Local backend provision variant for generic Next.js DB/auth briefs."""

from __future__ import annotations

import json

from skyn3t.agents._scaffold import _implies_local_backend, scaffold_for
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import proof_run


def test_local_backend_trigger_matches_generic_db_auth_phrases():
    for brief in (
        "a database-backed Next.js app",
        "an auth dashboard",
        "a member portal with login and saved projects",
    ):
        assert _implies_local_backend(brief), brief


def test_local_backend_trigger_ignores_plain_marketing_site_and_supabase():
    assert not _implies_local_backend("a Next.js blog")
    assert not _implies_local_backend("a Supabase auth dashboard")


def test_nextjs_generic_backend_brief_gets_local_backend_files():
    files = scaffold_for("nextjs", "member-portal", "a database-backed app with auth")
    assert "skyn3t-backend.json" in files
    assert "lib/localBackend.js" in files
    assert "app/api/auth/register/route.js" in files
    assert "app/api/auth/login/route.js" in files
    assert "app/api/items/route.js" in files
    assert "AUTH_SECRET" in files[".env.example"]
    manifest = json.loads(files["skyn3t-backend.json"])
    assert manifest["provider"] == "local-file"
    assert {"auth", "database"} <= set(manifest["capabilities"])


def test_nextjs_local_backend_variant_passes_structural_proof(tmp_path):
    files = scaffold_for("nextjs", "member-portal", "an auth dashboard")
    for rel, contents in files.items():
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)
    res = proof_run(tmp_path, checklist=file_checklist("nextjs"), stack="nextjs")
    assert res.passed, res.to_dict()
