from pathlib import Path

from skyn3t.worktree import PREVIEW_SUBDIR, sync_preview


def test_sync_preview_mirrors_worktree(tmp_path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "main.py").write_text("print('hi')\n")
    proj = tmp_path / "proj"

    copied = sync_preview(str(wt), str(proj))

    assert "src/main.py" in copied
    assert (proj / PREVIEW_SUBDIR / "src" / "main.py").read_text() == "print('hi')\n"


def test_sync_preview_replaces_stale_files(tmp_path):
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "a.py").write_text("a")
    proj = tmp_path / "proj"
    sync_preview(str(wt), str(proj))

    (wt / "a.py").unlink()
    (wt / "b.py").write_text("b")
    sync_preview(str(wt), str(proj))

    assert not (proj / PREVIEW_SUBDIR / "a.py").exists()  # clean replace
    assert (proj / PREVIEW_SUBDIR / "b.py").exists()
