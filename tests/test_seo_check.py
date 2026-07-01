"""Advisory SEO check — deterministic static scan of a delivered web build.

Mirrors tests/test_game_visual_check.py: inject content via ``tmp_path`` files, no
network, and pin the never-raise / degrade-open / advisory contract. SEO signals are
cheap and unambiguous, so this check is DETERMINISTIC (no LLM/vision) — the tests just
assert the static scan handles both literal HTML tags and the framework metadata idioms
(Next.js App Router ``metadata`` export) without false-flagging.
"""

from __future__ import annotations

from skyn3t.studio.seo_check import SeoVerdict, check_seo


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ── Next.js App Router: title/description/openGraph via a metadata export, the
#    <h1> in page.tsx, the html lang in the root layout — NO literal <title>/<meta>.
NEXT_LAYOUT = """
export const metadata = {
  title: "My Store",
  description: "The best store for everything you need",
  openGraph: { title: "My Store", description: "Shop the best products online" },
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
"""

NEXT_PAGE = """
export default function Home() {
  return (
    <main>
      <h1>Welcome to My Store</h1>
      <p>Shop the best products.</p>
    </main>
  );
}
"""


def test_complete_nextjs_app_router_is_ok(tmp_path):
    _write(tmp_path, "app/layout.tsx", NEXT_LAYOUT)
    _write(tmp_path, "app/page.tsx", NEXT_PAGE)
    v = check_seo(tmp_path, stack="nextjs")
    assert v.skipped is False
    assert v.ok is True
    assert v.issues == []  # metadata.title/description satisfy the check, not a literal <title>


FULL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>Cool Site</title>
  <meta name="description" content="A genuinely cool static site about things" />
  <meta property="og:title" content="Cool Site" />
  <meta property="og:description" content="A genuinely cool static site about things" />
</head>
<body>
  <h1>Cool Site</h1>
  <img src="hero.png" alt="a hero picture" />
</body>
</html>
"""


def test_complete_static_site_is_ok(tmp_path):
    _write(tmp_path, "index.html", FULL_HTML)
    _write(tmp_path, "public/robots.txt", "User-agent: *\nAllow: /\n")
    _write(tmp_path, "public/sitemap.xml", "<urlset></urlset>")
    v = check_seo(tmp_path, stack="static")
    assert v.ok is True
    assert v.issues == []
    assert v.warnings == []  # og present, all imgs have alt, robots + sitemap present


BARE_HTML = """<!DOCTYPE html>
<html>
<head></head>
<body>
  <p>hello world</p>
</body>
</html>
"""


def test_bare_html_missing_signals_flags_issues(tmp_path):
    _write(tmp_path, "index.html", BARE_HTML)
    v = check_seo(tmp_path, stack="static")
    assert v.skipped is False
    assert v.ok is False
    blob = " ".join(v.issues).lower()
    assert "title" in blob
    assert "description" in blob
    assert "h1" in blob
    assert "lang" in blob


IMG_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>Gallery</title>
  <meta name="description" content="An image gallery site with pictures" />
</head>
<body>
  <h1>Gallery</h1>
  <img src="a.png" alt="described" />
  <img src="b.png" />
  <img src="c.png">
</body>
</html>
"""


def test_images_without_alt_warn_with_count(tmp_path):
    _write(tmp_path, "index.html", IMG_HTML)
    v = check_seo(tmp_path, stack="static")
    assert v.ok is True  # missing alt is a soft WARN, never a hard issue
    assert v.checked["images_total"] == 3
    assert v.checked["images_missing_alt"] == 2
    blob = " ".join(v.warnings).lower()
    assert "alt" in blob and "2" in blob


def test_non_web_stack_is_skipped(tmp_path):
    _write(tmp_path, "index.html", FULL_HTML)
    for stack in ("python", "phaser"):
        v = check_seo(tmp_path, stack=stack)
        assert v.skipped is True
        assert v.ok is False
        assert v.gaps() == []  # a soft-skip never produces gaps (never false-flag)


def test_garbage_project_never_raises_and_skips(tmp_path):
    # A binary blob and no HTML-producing files -> degrade open (skip), never raise.
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02not html at all\xff")
    v = check_seo(tmp_path, stack="static")
    assert v.skipped is True
    # A path that isn't even a directory must also skip, not raise.
    v2 = check_seo(tmp_path / "does-not-exist", stack="static")
    assert v2.skipped is True


def test_gaps_returns_actionable_strings(tmp_path):
    _write(tmp_path, "index.html", BARE_HTML)
    v = check_seo(tmp_path, stack="static")
    gaps = v.gaps()
    assert gaps and all(isinstance(g, str) and g for g in gaps)
    joined = " ".join(gaps).lower()
    assert "title" in joined and "description" in joined and "h1" in joined and "lang" in joined


def test_clean_site_has_no_gaps(tmp_path):
    _write(tmp_path, "index.html", FULL_HTML)
    v = check_seo(tmp_path, stack="static")
    assert v.gaps() == []


def test_to_dict_shape(tmp_path):
    _write(tmp_path, "index.html", BARE_HTML)
    d = check_seo(tmp_path, stack="static").to_dict()
    for key in ("ok", "skipped", "issues", "warnings", "checked", "reason", "gaps"):
        assert key in d
    assert isinstance(d["issues"], list)
    assert isinstance(d["warnings"], list)
    assert isinstance(d["checked"], dict)
    assert isinstance(d["gaps"], list)


def test_skipped_verdict_is_advisory():
    # A soft-skip must be ok=False (not verified) yet produce NO gaps (degrade open).
    v = SeoVerdict(skipped=True, reason="not a web stack")
    assert v.ok is False
    assert v.gaps() == []
    assert v.to_dict()["skipped"] is True


def test_empty_stack_infers_from_files(tmp_path):
    # An empty stack string must not short-circuit: the scan still runs off the files.
    _write(tmp_path, "index.html", BARE_HTML)
    v = check_seo(tmp_path, stack="")
    assert v.skipped is False
    assert v.ok is False


# ── FIX 5a: Next.js `title: { default, template }` object form ───────────────
NEXT_TITLE_OBJECT = """
export const metadata = {
  title: { default: "My Store", template: "%s | My Store" },
  description: "The best store for everything you could ever need online",
  openGraph: { description: "Shop the best products online" },
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body><h1>My Store</h1>{children}</body>
    </html>
  );
}
"""


def test_nextjs_title_object_form_not_flagged(tmp_path):
    _write(tmp_path, "app/layout.tsx", NEXT_TITLE_OBJECT)
    v = check_seo(tmp_path, stack="nextjs")
    assert v.checked["title"] is True  # metadata.title object satisfies the check
    assert "title" not in " ".join(v.issues).lower()


NEXT_NO_TITLE = """
export const metadata = {
  description: "A description of the page content that is here",
};
"""


def test_nextjs_metadata_without_title_still_flags(tmp_path):
    _write(tmp_path, "app/layout.tsx", NEXT_NO_TITLE)
    v = check_seo(tmp_path, stack="nextjs")
    assert v.checked["title"] is False
    assert "title" in " ".join(v.issues).lower()


# ── FIX 5b: Remix `export const meta = () => [...]` idiom ─────────────────────
REMIX_ROOT = """
export const meta = () => [
  { title: "Remix Store" },
  { name: "description", content: "A great store built with the Remix framework" },
];

export default function App() {
  return (
    <html lang="en">
      <body><h1>Remix Store</h1></body>
    </html>
  );
}
"""


def test_remix_meta_export_not_flagged(tmp_path):
    _write(tmp_path, "app/root.tsx", REMIX_ROOT)
    v = check_seo(tmp_path, stack="remix")
    assert v.checked["title"] is True
    assert v.checked["description"] is True
    assert v.ok is True


REMIX_NO_DESC = """
import type { MetaFunction } from "@remix-run/node";
export const meta: MetaFunction = () => [{ title: "Only A Title Here" }];
"""


def test_remix_meta_export_missing_description_flags(tmp_path):
    _write(tmp_path, "app/routes/_index.tsx", REMIX_NO_DESC)
    v = check_seo(tmp_path, stack="remix")
    assert v.checked["title"] is True
    assert v.checked["description"] is False
    assert "description" in " ".join(v.issues).lower()


# ── FIX 5c: tighten the metadata-source context gate ─────────────────────────
STRAY_TITLE_CONFIG = """
// metadata about the book chapters below
export const config = { title: "Not a real page title", helmet: false };
"""


def test_stray_title_in_non_metadata_source_not_counted(tmp_path):
    # A bare "metadata"/"helmet" word in a comment must not turn an unrelated
    # config object's title: into a page title.
    _write(tmp_path, "data/config.ts", STRAY_TITLE_CONFIG)
    v = check_seo(tmp_path, stack="nextjs")
    assert v.checked["title"] is False
    assert "title" in " ".join(v.issues).lower()


def test_real_metadata_export_is_recognized(tmp_path):
    _write(tmp_path, "app/layout.tsx",
           'export const metadata = { title: "Real", '
           'description: "A real and non-empty page description here" };')
    v = check_seo(tmp_path, stack="nextjs")
    assert v.checked["title"] is True and v.checked["description"] is True


# ── FIX 5d: openGraph key search is brace-bounded ────────────────────────────
OG_BLOCK_NO_KEY = """
export const metadata = {
  title: "Present", description: "A page description that is present and non-empty",
  openGraph: { images: [{ url: "/og.png", width: 1200 }] },
  other: { title: "not an OG title", description: "not an OG description" },
};
"""


def test_opengraph_key_search_is_brace_bounded(tmp_path):
    # The og block has neither title nor description; a later unrelated
    # title:/description: must not be attributed to openGraph.
    _write(tmp_path, "app/layout.tsx", OG_BLOCK_NO_KEY)
    v = check_seo(tmp_path, stack="nextjs")
    assert v.checked["og_title"] is False
    assert v.checked["og_description"] is False


OG_BLOCK_WITH_KEYS = """
export const metadata = {
  title: "T", description: "D that is non empty and present here",
  openGraph: { title: "OG Title", description: "OG description here",
               images: [{ url: "/x.png" }] },
};
"""


def test_opengraph_keys_inside_block_detected(tmp_path):
    _write(tmp_path, "app/layout.tsx", OG_BLOCK_WITH_KEYS)
    v = check_seo(tmp_path, stack="nextjs")
    assert v.checked["og_title"] is True
    assert v.checked["og_description"] is True


# ── FIX 5e: build-output HTML is not scanned for source signals ──────────────
def test_build_output_html_is_not_scanned(tmp_path):
    # Only a built .next page, no delivered SOURCE pages -> degrade open (skip).
    _write(tmp_path, ".next/server/app/index.html", FULL_HTML)
    v = check_seo(tmp_path, stack="nextjs")
    assert v.skipped is True


def test_stale_build_html_does_not_satisfy_source_miss(tmp_path):
    _write(tmp_path, "index.html", BARE_HTML)       # delivered source, all missing
    _write(tmp_path, "dist/index.html", FULL_HTML)  # stale build output with signals
    v = check_seo(tmp_path, stack="static")
    assert v.ok is False
    blob = " ".join(v.issues).lower()
    assert "title" in blob and "description" in blob and "h1" in blob and "lang" in blob


# ── FIX 5f: node_modules is pruned (perf + never a signal source) ────────────
def test_node_modules_title_not_counted(tmp_path):
    _write(tmp_path, "node_modules/pkg/index.html",
           "<html lang=en><head><title>Dep</title></head><body><h1>x</h1></body></html>")
    _write(tmp_path, "index.html", BARE_HTML)
    v = check_seo(tmp_path, stack="static")
    assert v.checked["title"] is False  # a dependency's HTML must not provide the signal
    assert "title" in " ".join(v.issues).lower()
