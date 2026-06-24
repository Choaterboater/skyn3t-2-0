"""Each build gets its OWN folder — a redo (or a concurrent build of the same
brief) must never inherit or mix with a previous run's files.

Observed: every 'kids coloring app' build wrote into ONE shared slug dir, so old
stub files and new real files mixed and a good build looked broken.
"""

from __future__ import annotations

from skyn3t.config.settings import Settings
from skyn3t.studio.runner import StudioRunner


def _runner(tmp_path):
    r = StudioRunner.__new__(StudioRunner)
    r.settings = Settings(projects_dir=tmp_path / "Projects",
                          data_dir=tmp_path / "d", logs_dir=tmp_path / "l")
    return r


def test_first_build_keeps_plain_slug(tmp_path):
    r = _runner(tmp_path)
    assert r._reserve_unique_slug("coloring-app") == "coloring-app"
    assert (r.settings.projects_dir / "coloring-app").is_dir()  # reserved on disk


def test_redo_gets_a_new_folder(tmp_path):
    r = _runner(tmp_path)
    a = r._reserve_unique_slug("coloring-app")   # coloring-app
    b = r._reserve_unique_slug("coloring-app")   # must differ
    c = r._reserve_unique_slug("coloring-app")
    assert a == "coloring-app" and b == "coloring-app-2" and c == "coloring-app-3"
    assert a != b != c
    for s in (a, b, c):
        assert (r.settings.projects_dir / s).is_dir()


def test_existing_polluted_dir_is_skipped(tmp_path):
    r = _runner(tmp_path)
    (r.settings.projects_dir / "coloring-app").mkdir(parents=True)  # pre-existing mess
    (r.settings.projects_dir / "coloring-app" / "stale.txt").write_text("old")
    got = r._reserve_unique_slug("coloring-app")
    assert got == "coloring-app-2"  # the new build avoids the polluted dir
