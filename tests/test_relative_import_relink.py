from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.studio.proof_run import (
    _unresolved_local_imports,
    apply_deterministic_repairs,
    relink_unresolved_relative_imports,
)


def _write(root: Path, relative: str, content: str = "export default {}\n") -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def test_repairs_nested_astro_component_lib_and_layout_imports(tmp_path: Path) -> None:
    _write(tmp_path, "src/layouts/BaseLayout.astro", "<slot />\n")
    _write(tmp_path, "src/components/LessonCard.astro", "<article><slot /></article>\n")
    _write(tmp_path, "src/lib/content.ts", "export const lessons = [];\n")
    importer = _write(
        tmp_path,
        "src/pages/drills/index.astro",
        """---
import BaseLayout from '../layouts/BaseLayout.astro';
import LessonCard from '../components/LessonCard.astro';
import { lessons } from '../lib/content';
---
<BaseLayout><LessonCard /></BaseLayout>
""",
    )

    assert len(_unresolved_local_imports(tmp_path)) == 3

    repairs = apply_deterministic_repairs(tmp_path, stack="astro")

    text = importer.read_text(encoding="utf-8")
    assert "from '../../layouts/BaseLayout.astro'" in text
    assert "from '../../components/LessonCard.astro'" in text
    assert "from '../../lib/content'" in text
    assert repairs["imports_relinked"] == [
        "src/pages/drills/index.astro: ../layouts/BaseLayout.astro -> "
        "../../layouts/BaseLayout.astro",
        "src/pages/drills/index.astro: ../components/LessonCard.astro -> "
        "../../components/LessonCard.astro",
        "src/pages/drills/index.astro: ../lib/content -> ../../lib/content",
    ]
    assert repairs["imports_scaffolded"] == []
    assert _unresolved_local_imports(tmp_path) == []


@pytest.mark.parametrize(
    ("importer_name", "target_name", "old_spec", "new_spec", "before", "after"),
    [
        (
            "model.ts",
            "value.ts",
            "../lib/value",
            "../../lib/value",
            "import { value } from '../lib/value';\n",
            "import { value } from '../../lib/value';\n",
        ),
        (
            "model.js",
            "value.js",
            "../lib/value.js",
            "../../lib/value.js",
            "const value = require('../lib/value.js');\n",
            "const value = require('../../lib/value.js');\n",
        ),
    ],
)
def test_relinks_nested_js_ts_import_without_changing_syntax(
    tmp_path: Path,
    importer_name: str,
    target_name: str,
    old_spec: str,
    new_spec: str,
    before: str,
    after: str,
) -> None:
    _write(tmp_path, f"src/lib/{target_name}", "export const value = 1;\n")
    importer = _write(tmp_path, f"src/features/dashboard/{importer_name}", before)

    evidence = relink_unresolved_relative_imports(tmp_path)

    assert importer.read_text(encoding="utf-8") == after
    assert evidence == [
        f"src/features/dashboard/{importer_name}: {old_spec} -> {new_spec}"
    ]


def test_ambiguous_duplicate_basename_is_not_relinked(tmp_path: Path) -> None:
    _write(tmp_path, "src/components/Card.astro", "<article />\n")
    _write(tmp_path, "src/legacy/Card.astro", "<aside />\n")
    importer = _write(
        tmp_path,
        "src/pages/drills/index.astro",
        "---\nimport Card from '../components/Card.astro';\n---\n<Card />\n",
    )
    before = importer.read_text(encoding="utf-8")

    assert relink_unresolved_relative_imports(tmp_path) == []
    assert importer.read_text(encoding="utf-8") == before
    assert _unresolved_local_imports(tmp_path)


def test_already_valid_relative_import_is_untouched(tmp_path: Path) -> None:
    _write(tmp_path, "src/layouts/BaseLayout.astro", "<slot />\n")
    importer = _write(
        tmp_path,
        "src/pages/index.astro",
        "---\nimport BaseLayout from '../layouts/BaseLayout.astro';\n---\n<BaseLayout />\n",
    )
    before = importer.read_bytes()

    assert relink_unresolved_relative_imports(tmp_path) == []
    assert importer.read_bytes() == before
    assert _unresolved_local_imports(tmp_path) == []


def test_escaping_relative_import_is_not_redirected_inside_project(tmp_path: Path) -> None:
    _write(tmp_path, "src/lib/content.ts", "export const content = [];\n")
    importer = _write(
        tmp_path,
        "src/pages/drills/index.astro",
        "---\nimport { content } from '../../../../lib/content.ts';\n---\n",
    )
    before = importer.read_bytes()

    assert relink_unresolved_relative_imports(tmp_path) == []
    assert importer.read_bytes() == before
