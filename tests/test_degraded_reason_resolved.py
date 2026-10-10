"""code_degraded must reflect the DELIVERED tree, not a codegen-time snapshot.

bellwork-v2 (2026-10-09): the stall guard cut Opus off mid-slice, so codegen
recorded "missing 1 owned file(s): app/globals.css". The fix-loop then wrote
app/globals.css, `next build` passed, the app rendered — and the build was still
no_go because the degraded flag was never re-checked against the disk."""
from __future__ import annotations

from skyn3t.studio.runner import degraded_reason_resolved

_REASON = ("frontend: agentic slice 'frontend' incomplete after 1 retry: provider did "
           "not confirm completion: agentic CLI ended without a successful result; "
           "missing 1 owned file(s): app/globals.css")


def test_resolved_when_every_missing_file_now_exists(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "globals.css").write_text(":root{--bg:#fff}\n")
    assert degraded_reason_resolved(_REASON, tmp_path) is True


def test_unresolved_while_file_still_missing_or_empty(tmp_path):
    assert degraded_reason_resolved(_REASON, tmp_path) is False
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "globals.css").write_text("  \n")
    assert degraded_reason_resolved(_REASON, tmp_path) is False


def test_multi_slice_all_must_resolve(tmp_path):
    reason = (_REASON + "; backend: completion slice 'backend' missed 2 owned file(s): "
              "lib/a.js, lib/b.js")
    (tmp_path / "app").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "app" / "globals.css").write_text("a{}\n")
    (tmp_path / "lib" / "a.js").write_text("export const a=1\n")
    assert degraded_reason_resolved(reason, tmp_path) is False
    (tmp_path / "lib" / "b.js").write_text("export const b=1\n")
    assert degraded_reason_resolved(reason, tmp_path) is True


def test_truncated_list_cannot_be_verified(tmp_path):
    names = [f"f{i}.js" for i in range(12)]
    for n in names:
        (tmp_path / n).write_text("x=1\n")
    reason = "slice 's' incomplete: missing 14 owned file(s): " + ", ".join(names)
    assert degraded_reason_resolved(reason, tmp_path) is False


def test_non_file_reasons_never_resolve(tmp_path):
    for reason in ("agentic build under-delivered after 1 retry",
                   "frontend: slice produced no files",
                   "frontend: agentic slice 'frontend' incomplete after 1 retry: "
                   "provider did not confirm completion",
                   "x: missing 0 owned file(s): ; rejected prose source: app/page.jsx",
                   ""):
        assert degraded_reason_resolved(reason, tmp_path) is False, reason
