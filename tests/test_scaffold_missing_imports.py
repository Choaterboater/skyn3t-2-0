"""scaffold_missing_imports — generate stubs for local imports whose target file
was never created (the recurring '@/components/ui/button -> Module not found'
codegen break), so the build resolves instead of no_go-ing."""
from __future__ import annotations

import json

from skyn3t.studio.proof_run import (
    _invalid_npm_package_names,
    scaffold_missing_imports,
)


def _nextish(tmp_path):
    (tmp_path / "jsconfig.json").write_text(json.dumps(
        {"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}}}), encoding="utf-8")
    (tmp_path / "components").mkdir()


def test_scaffolds_missing_alias_component(tmp_path):
    _nextish(tmp_path)
    (tmp_path / "components" / "Navbar.jsx").write_text(
        "import { Button } from '@/components/ui/button'\n"
        "import { cn } from '@/lib/utils'\n"
        "export default function Navbar(){ return <Button/> }\n", encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert "components/ui/button.jsx" in written
    assert any("lib/utils" in w for w in written)
    btn = (tmp_path / "components/ui/button.jsx").read_text()
    assert "export function Button" in btn and "'button'" in btn  # real <button> element
    util = (tmp_path / "lib/utils.js").read_text()
    assert "export const cn" in util


def test_scaffolds_missing_default_and_relative(tmp_path):
    _nextish(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "import Hero from '../components/Hero'\n"
        "export default function Page(){ return <Hero/> }\n", encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert any("components/Hero" in w for w in written)
    hero = (tmp_path / "components/Hero.jsx").read_text()
    assert "export default" in hero


def test_does_not_touch_existing_or_bare_imports(tmp_path):
    _nextish(tmp_path)
    (tmp_path / "components" / "Card.jsx").write_text("export default function Card(){return null}\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "import React from 'react'\n"                       # bare pkg -> ignored
        "import Card from '@/components/Card'\n"            # exists -> ignored
        "export default function P(){return <Card/>}\n", encoding="utf-8")
    assert scaffold_missing_imports(tmp_path) == []


def test_longest_alias_prefix_wins(tmp_path):
    """Multi-pattern jsconfig (@/* -> ./src/* AND @/components/* -> ./components/*):
    a component that exists under the LONGER prefix's dir must resolve (no stub),
    matching webpack/tsc longest-prefix resolution."""
    (tmp_path / "jsconfig.json").write_text(json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {
        "@/*": ["./src/*"], "@/components/*": ["./components/*"]}}}), encoding="utf-8")
    (tmp_path / "components").mkdir()
    (tmp_path / "components" / "Hero.jsx").write_text(
        "export default function Hero(){return null}\n", encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "import Hero from '@/components/Hero'\nexport default function P(){return <Hero/>}\n", encoding="utf-8")
    assert scaffold_missing_imports(tmp_path) == []


def test_longest_alias_prefix_stub_location(tmp_path):
    """A genuinely-missing @/components/* import is stubbed under components/, NOT
    src/components/ (where the old first-match bug wrongly placed it)."""
    (tmp_path / "jsconfig.json").write_text(json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {
        "@/*": ["./src/*"], "@/components/*": ["./components/*"]}}}), encoding="utf-8")
    (tmp_path / "components").mkdir()
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "import Hero from '@/components/Hero'\nexport default function P(){return <Hero/>}\n", encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert any("components/Hero" in w for w in written)
    assert (tmp_path / "components" / "Hero.jsx").exists()
    assert not (tmp_path / "src" / "components" / "Hero.jsx").exists()


def test_invalid_npm_names_rejects_template_fragment():
    # '${r}' scraped from minified code must be rejected (it broke a real build)
    flagged = _invalid_npm_package_names({"dependencies": {"${r}": "latest", "react": "^18"}})
    assert "${r}" in flagged and "react" not in flagged


# ── extension-qualified relative imports (real shipped bug) ────────────────────
# A real deepseek-routed build shipped `import { PreloadScene } from
# './PreloadScene.js'` in src/main.js with PreloadScene.js never written. The
# repair mechanism DETECTED it correctly but stubbed the WRONG filename
# (PreloadScene.js.jsx instead of PreloadScene.js) because it didn't strip the
# extension the import spec already carried before appending its own -- the
# literal string the bundler looks for was never created, so the app still
# couldn't boot even after "repair."

def test_stub_reuses_the_specs_own_extension_instead_of_doubling_it(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text(
        "import { PreloadScene } from './PreloadScene.js';\n"
        "export default PreloadScene;\n", encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert "src/PreloadScene.js" in written
    assert (tmp_path / "src" / "PreloadScene.js").exists()
    assert not (tmp_path / "src" / "PreloadScene.js.js").exists()
    assert not (tmp_path / "src" / "PreloadScene.js.jsx").exists()


def test_stub_reuses_ts_extension_too(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text(
        "import { helper } from './helper.ts';\nhelper();\n", encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert "src/helper.ts" in written
    assert (tmp_path / "src" / "helper.ts").exists()
    assert not (tmp_path / "src" / "helper.ts.ts").exists()


def test_extensionless_import_still_gets_an_inferred_extension(tmp_path):
    # Control: the ORIGINAL behavior (no extension in the spec) must be unchanged.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text(
        "import Hero from './Hero';\nexport default Hero;\n", encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert "src/Hero.jsx" in written
    assert (tmp_path / "src" / "Hero.jsx").exists()


# ── stack-aware stub content (real shipped bug, part 2) ─────────────────────────
# Even with the filename fixed, scaffold_missing_imports accepted a `stack`
# kwarg but never used it -- every stub was unconditionally React/JSX-shaped
# (`import React from 'react'; ... React.createElement(...)`), which would
# inject a bogus React dependency into a non-React project (e.g. a Phaser
# scene) even after the filename bug is fixed.

def test_phaser_stack_stub_has_no_react_import(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text(
        "import { PreloadScene } from './PreloadScene.js';\n"
        "export default PreloadScene;\n", encoding="utf-8")
    scaffold_missing_imports(tmp_path, stack="phaser")
    content = (tmp_path / "src" / "PreloadScene.js").read_text()
    assert "react" not in content.lower()


def test_react_stack_stub_still_uses_react(tmp_path):
    # Control: explicit react-family stacks keep the existing React-shaped stub.
    _nextish(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "import Hero from '../components/Hero'\n"
        "export default function Page(){ return <Hero/> }\n", encoding="utf-8")
    scaffold_missing_imports(tmp_path, stack="nextjs")
    content = (tmp_path / "components" / "Hero.jsx").read_text()
    assert "react" in content.lower()


# ── stylesheet-extension targets (real gap: scaffold_missing_imports wrote JS
# content into a .css-named file) ───────────────────────────────────────────
# A dangling `import './styles/app.css'` resolved to the right FILENAME (once
# the extension-duplication bug was fixed) but still got JS/React-shaped
# CONTENT written into it (e.g. `export {};` or `import React from 'react'`) —
# nonsensical for a stylesheet and likely to fail a CSS-processor (PostCSS)
# parse step even though the bundler's own import-resolution now succeeds.
# scaffold_missing_imports must special-case stylesheet extensions the same
# way the (now-redundant) dedicated _stub_dangling_stylesheets did: an empty,
# syntactically valid CSS stub, not JS content.

def test_dangling_stylesheet_gets_valid_css_content_not_js(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.jsx").write_text(
        "import './styles/app.css';\nexport default function App(){return null}\n",
        encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert "src/styles/app.css" in written
    content = (tmp_path / "src" / "styles" / "app.css").read_text()
    assert "react" not in content.lower()
    assert "export" not in content
    assert "import '" not in content and 'import "' not in content  # no JS import statement


def test_dangling_scss_also_gets_css_content(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.jsx").write_text(
        "import './theme.scss';\nexport default function App(){return null}\n",
        encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert "src/theme.scss" in written
    content = (tmp_path / "src" / "theme.scss").read_text()
    assert "react" not in content.lower() and "export" not in content


def test_bare_side_effect_js_import_is_also_detected(tmp_path):
    # The gap this fix closes generalizes beyond stylesheets: ANY bare import
    # (no `from` clause) was previously invisible to scaffold_missing_imports.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.js").write_text(
        "import './polyfills.js';\nconsole.log('ready');\n", encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert "src/polyfills.js" in written
    assert (tmp_path / "src" / "polyfills.js").exists()


def test_spec_matched_by_named_import_is_not_double_stubbed_by_bare_pass(tmp_path):
    # A `from`-clause import is handled by the first (named-import) pass; the
    # second (bare-import) pass must not re-process the same spec.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.jsx").write_text(
        "import Hero from './Hero';\nexport default function App(){return null}\n",
        encoding="utf-8")
    written = scaffold_missing_imports(tmp_path)
    assert written.count("src/Hero.jsx") == 1


# ── out-of-tree write confinement (real pre-existing security gap) ─────────────
# Discovered while retiring the dedicated _stub_dangling_stylesheets mechanism:
# it had its OWN confinement check (never write outside the project root for an
# escaping '../../x' relative import) that scaffold_missing_imports never had at
# all — for ANY import, not just stylesheets. A '../../sibling/theme.css'-style
# spec would resolve OUTSIDE the project root and get a stub file written there,
# a real path-traversal / arbitrary-file-write bug (silently "fixing" the import
# by polluting a sibling directory, e.g. another project's tree, instead of
# leaving it correctly flagged as unresolved/no_go).

def test_escaping_relative_import_is_not_stubbed_out_of_tree(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (proj / "src" / "main.jsx").write_text(
        "import './../../sibling/theme.css';\nexport default function App(){return null}\n",
        encoding="utf-8")
    written = scaffold_missing_imports(proj)
    assert written == []
    assert not (sibling / "theme.css").exists()


def test_escaping_named_import_is_not_stubbed_out_of_tree(tmp_path):
    # Same confinement guarantee for the named-import (has a `from` clause) pass.
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    (proj / "src" / "main.jsx").write_text(
        "import Evil from '../../sibling/Evil';\nexport default function App(){return null}\n",
        encoding="utf-8")
    written = scaffold_missing_imports(proj)
    assert written == []
    assert not any(sibling.iterdir())


def test_in_tree_deeply_nested_relative_import_still_works(tmp_path):
    # Control: confinement must not over-block legitimate in-tree traversal.
    proj = tmp_path / "proj"
    (proj / "src" / "features" / "auth").mkdir(parents=True)
    (proj / "src" / "features" / "auth" / "Login.jsx").write_text(
        "import Shared from '../../shared/Shared';\nexport default function L(){return null}\n",
        encoding="utf-8")
    written = scaffold_missing_imports(proj)
    assert "src/shared/Shared.jsx" in written
    assert (proj / "src" / "shared" / "Shared.jsx").exists()


def test_no_stack_specified_defaults_to_react_shape_for_backward_compat(tmp_path):
    # Control: existing callers that don't pass `stack` must see unchanged behavior.
    _nextish(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "page.jsx").write_text(
        "import Hero from '../components/Hero'\n"
        "export default function Page(){ return <Hero/> }\n", encoding="utf-8")
    scaffold_missing_imports(tmp_path)
    content = (tmp_path / "components" / "Hero.jsx").read_text()
    assert "react" in content.lower()
