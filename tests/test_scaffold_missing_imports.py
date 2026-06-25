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


def test_invalid_npm_names_rejects_template_fragment():
    # '${r}' scraped from minified code must be rejected (it broke a real build)
    flagged = _invalid_npm_package_names({"dependencies": {"${r}": "latest", "react": "^18"}})
    assert "${r}" in flagged and "react" not in flagged
