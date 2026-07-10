"""slice_plan — decompose an architect manifest into parallel build slices.

Pure/offline assertions: classification by path, the min-files and
single-slice guards (which keep the monolithic path), merge ordering (config
last), and the per-slice tier hints.
"""

from __future__ import annotations

import pytest

from skyn3t.studio.slicer import SLICE_TIERS, slice_plan, slice_tier

# A realistic full-stack React + FastAPI manifest (>= min_files).
_FULLSTACK = [
    {"path": "src/App.jsx", "purpose": "root"},
    {"path": "src/main.jsx", "purpose": "entry"},
    {"path": "src/components/Card.jsx", "purpose": "ui"},
    {"path": "src/styles/index.css", "purpose": "styles"},
    {"path": "index.html", "purpose": "html shell"},
    {"path": "package.json", "purpose": "manifest"},
    {"path": "vite.config.js", "purpose": "build config"},
    {"path": "api/main.py", "purpose": "fastapi app"},
    {"path": "api/routes/users.py", "purpose": "routes"},
    {"path": "tests/test_users.py", "purpose": "backend tests"},
]


def test_fullstack_splits_into_expected_slices():
    sl = slice_plan(_FULLSTACK, stack="react")
    assert set(sl) == {"frontend", "backend", "tests", "config"}
    paths = {name: [f["path"] for f in files] for name, files in sl.items()}
    assert "src/App.jsx" in paths["frontend"]
    assert "src/styles/index.css" in paths["frontend"]
    assert "api/main.py" in paths["backend"]
    assert "api/routes/users.py" in paths["backend"]
    assert "tests/test_users.py" in paths["tests"]
    assert "package.json" in paths["config"]
    assert "vite.config.js" in paths["config"]
    assert "index.html" in paths["config"]


def test_config_slice_merges_last():
    # The mapping is ordered so config (authoritative) is iterated/merged last.
    sl = slice_plan(_FULLSTACK, stack="react")
    assert list(sl)[-1] == "config"


def test_below_min_files_keeps_monolithic():
    # A 3-file app isn't worth slicing -> empty dict -> caller stays monolithic.
    tiny = [{"path": "src/App.jsx"}, {"path": "src/main.jsx"}, {"path": "index.html"}]
    assert slice_plan(tiny, stack="react") == {}


def test_single_slice_keeps_monolithic():
    # Eight pure-python files all land in "backend": one slice, nothing to
    # parallelise -> empty dict.
    only_backend = [{"path": f"pkg/mod_{i}.py"} for i in range(8)]
    assert slice_plan(only_backend, stack="python") == {}


def test_accepts_plain_path_strings_and_dedupes():
    files = ["src/App.jsx", "src/App.jsx", "api/main.py", "package.json",
             "src/a.jsx", "src/b.jsx", "src/c.css", "api/db.py", "tests/test_x.py"]
    sl = slice_plan(files, stack="react")
    # Deduped App.jsx appears once.
    assert [f["path"] for f in sl["frontend"]].count("src/App.jsx") == 1
    assert "api/main.py" in [f["path"] for f in sl["backend"]]


def test_normalizes_paths_before_assigning_slice_ownership():
    files = [
        {"path": r"src\App.jsx", "purpose": "canonical owner"},
        {"path": "SRC/app.jsx", "purpose": "case alias"},
        {"path": "src/./components//Card.jsx", "purpose": "card"},
        {"path": "package.json", "purpose": "manifest"},
    ]

    sl = slice_plan(files, stack="react", min_files=2)
    frontend = sl["frontend"]
    paths = [entry["path"] for entry in frontend]

    assert paths == ["src/App.jsx", "src/components/Card.jsx"]
    assert frontend[0]["purpose"] == "canonical owner"
    assert sl["config"][0]["path"] == "package.json"


@pytest.mark.parametrize(
    "unsafe",
    [
        "",
        "   ",
        ".",
        "..",
        "../outside.py",
        "src/../outside.py",
        "/absolute.py",
        r"\absolute.py",
        r"C:\outside.py",
        "file:stream.py",
        "nul\x00byte.py",
    ],
)
def test_rejects_unsafe_architect_paths(unsafe):
    sl = slice_plan(
        [unsafe, {"path": "src/App.jsx"}, {"path": "package.json"}],
        stack="react",
        min_files=2,
    )

    paths = {
        entry["path"]
        for entries in sl.values()
        for entry in entries
    }
    assert paths == {"src/App.jsx", "package.json"}


def test_case_and_separator_aliases_count_once_for_minimum():
    files = [
        "src/App.jsx",
        "SRC/APP.JSX",
        r"src\App.jsx",
        "package.json",
    ]

    assert slice_plan(files, stack="react", min_files=3) == {}


def test_ambiguous_js_defaults_by_stack():
    # A bare index.js with no dir signal: frontend for a web stack, backend for a
    # node stack. Each manifest keeps >=2 slices so the guard doesn't collapse it.
    web = slice_plan([
        {"path": "src/App.jsx"}, {"path": "src/main.jsx"}, {"path": "src/Card.jsx"},
        {"path": "src/index.css"}, {"path": "api/main.py"}, {"path": "api/db.py"},
        {"path": "package.json"}, {"path": "tests/test_a.py"}, {"path": "index.js"},
    ], stack="react")
    assert "index.js" in [f["path"] for f in web["frontend"]]

    node = slice_plan([
        {"path": "src/App.jsx"}, {"path": "src/b.jsx"}, {"path": "src/index.css"},
        {"path": "api/main.py"}, {"path": "api/db.py"}, {"path": "package.json"},
        {"path": "tests/test_a.py"}, {"path": "index.js"},
    ], stack="node")
    assert "index.js" in [f["path"] for f in node["backend"]]


def test_slice_tier_hints():
    assert slice_tier("frontend") == "ui"
    assert slice_tier("frontend_pages") == "ui"
    assert slice_tier("frontend_components") == "ui"
    assert slice_tier("backend") == "strong"
    assert slice_tier("config") == "cheap"
    assert slice_tier("tests") == "cheap"
    assert slice_tier("unknown") == "backend"
    assert set(SLICE_TIERS) == {"frontend", "backend", "tests", "config"}


def test_full_app_frontend_subslices_by_path_without_losing_files():
    files = [
        {"path": "src/data/site.ts", "purpose": "shared content"},
        {"path": "src/content/lessons.json", "purpose": "lesson catalog"},
        {"path": "src/components/LessonCard.astro", "purpose": "lesson card"},
        {"path": "src/layouts/PageLayout.astro", "purpose": "shared layout"},
        {"path": "src/pages/index.astro", "purpose": "home route"},
        {"path": "src/pages/lessons/[slug].astro", "purpose": "lesson route"},
        {"path": "src/styles/global.css", "purpose": "global styles"},
        {"path": "src/lib/navigation.ts", "purpose": "frontend wiring"},
        {"path": "tests/routes.test.ts", "purpose": "route test"},
        {"path": "package.json", "purpose": "manifest"},
    ]

    slices = slice_plan(
        files,
        stack="astro",
        min_files=4,
        semantic_frontend=True,
    )

    assert list(slices) == [
        "frontend_content",
        "frontend_components",
        "frontend_pages",
        "frontend_styles",
        "frontend_core",
        "tests",
        "config",
    ]
    paths = {
        name: [entry["path"] for entry in entries]
        for name, entries in slices.items()
    }
    assert paths["frontend_content"] == [
        "src/data/site.ts",
        "src/content/lessons.json",
    ]
    assert paths["frontend_components"] == [
        "src/components/LessonCard.astro",
        "src/layouts/PageLayout.astro",
    ]
    assert paths["frontend_pages"] == [
        "src/pages/index.astro",
        "src/pages/lessons/[slug].astro",
    ]
    assert paths["frontend_styles"] == ["src/styles/global.css"]
    assert paths["frontend_core"] == ["src/lib/navigation.ts"]
    assigned = [entry["path"] for entries in slices.values() for entry in entries]
    assert assigned == [
        "src/data/site.ts",
        "src/content/lessons.json",
        "src/components/LessonCard.astro",
        "src/layouts/PageLayout.astro",
        "src/pages/index.astro",
        "src/pages/lessons/[slug].astro",
        "src/styles/global.css",
        "src/lib/navigation.ts",
        "tests/routes.test.ts",
        "package.json",
    ]
    assert len(assigned) == len(set(assigned)) == len(files)


def test_semantic_frontend_is_opt_in_and_small_build_guard_still_wins():
    files = [
        {"path": "src/components/Card.jsx"},
        {"path": "src/pages/Home.jsx"},
        {"path": "src/styles.css"},
        {"path": "package.json"},
    ]

    default = slice_plan(files, stack="react", min_files=4)
    assert set(default) == {"frontend", "config"}
    assert slice_plan(
        files,
        stack="react",
        min_files=5,
        semantic_frontend=True,
    ) == {}


def test_semantic_frontend_can_parallelize_a_frontend_only_full_app():
    files = [
        {"path": "src/data/catalog.ts"},
        {"path": "src/components/Card.jsx"},
        {"path": "src/components/Filter.jsx"},
        {"path": "src/pages/Home.jsx"},
        {"path": "src/pages/Detail.jsx"},
        {"path": "src/styles/global.css"},
        {"path": "src/App.jsx"},
        {"path": "src/main.jsx"},
    ]

    assert slice_plan(files, stack="react") == {}
    slices = slice_plan(files, stack="react", semantic_frontend=True)
    assert list(slices) == [
        "frontend_content",
        "frontend_components",
        "frontend_pages",
        "frontend_styles",
        "frontend_core",
    ]
    assert sum(len(entries) for entries in slices.values()) == len(files)


def test_static_service_html_is_a_frontend_page_not_backend_service():
    slices = slice_plan(
        [
            {"path": "index.html"},
            {"path": "services/heating.html"},
            {"path": "services/emergency.html"},
            {"path": "services/quotes.py"},
            {"path": "partials/header.html"},
            {"path": "assets/css/pages.css"},
            {"path": "data/site.js"},
            {"path": "main.js"},
            {"path": "package.json"},
        ],
        stack="static",
        min_files=4,
        semantic_frontend=True,
    )

    assert [entry["path"] for entry in slices["frontend_pages"]] == [
        "index.html",
        "services/heating.html",
        "services/emergency.html",
    ]
    assert [entry["path"] for entry in slices["backend"]] == ["services/quotes.py"]
    assert list(slices)[-1] == "config"
