"""Vue, SvelteKit, and TypeScript-first web stacks.

Pins the three vocabulary layers that must agree for a new web stack:
planner keywords/checklists, agent stack normalization/detection, and a real
offline scaffold builder. The proof check stays local/offline.
"""

from __future__ import annotations

import json

from skyn3t.agents._common import KNOWN_STACKS, _normalize_stack
from skyn3t.agents._common import detect_stack as agent_detect_stack
from skyn3t.agents._scaffold import scaffold_for
from skyn3t.intelligence.skill_library import _stack_aliases
from skyn3t.studio.planner import detect_stack as plan_detect_stack
from skyn3t.studio.planner import file_checklist
from skyn3t.studio.proof_run import _NODE_STACKS, proof_run


def _write(files: dict[str, str], root) -> None:
    for rel, contents in files.items():
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(contents)


def test_planner_detects_new_web_frameworks():
    assert plan_detect_stack("build a Vue dashboard") == "vue"
    assert plan_detect_stack("build a SvelteKit customer portal") == "sveltekit"
    assert plan_detect_stack("build a TypeScript React web app") == "react_ts"


def test_common_normalizes_and_detects_new_web_frameworks():
    assert {"vue", "sveltekit", "react_ts"} <= set(KNOWN_STACKS)
    assert _normalize_stack("vue.js") == "vue"
    assert _normalize_stack("svelte kit") == "sveltekit"
    assert _normalize_stack("react typescript") == "react_ts"
    assert agent_detect_stack(brief="a vue admin app") == "vue"
    assert agent_detect_stack(brief="a sveltekit app") == "sveltekit"
    assert agent_detect_stack(brief="a vite react typescript app") == "react_ts"


def test_vue_scaffold_is_runnable_shape():
    files = scaffold_for("vue", "demo", "a Vue dashboard")
    for required in ("package.json", "index.html", "vite.config.ts", "src/main.ts", "src/App.vue", "README.md"):
        assert required in files, f"{required} missing from vue scaffold"
    pkg = json.loads(files["package.json"])
    assert "vue" in pkg["dependencies"]
    assert "@vitejs/plugin-vue" in pkg["devDependencies"]
    assert "build" in pkg["scripts"]
    assert "<script setup lang=\"ts\">" in files["src/App.vue"]
    assert "React" not in files["src/App.vue"]


def test_sveltekit_scaffold_is_runnable_shape():
    files = scaffold_for("sveltekit", "demo", "a SvelteKit portal")
    for required in ("package.json", "svelte.config.js", "vite.config.ts", "src/routes/+page.svelte", "README.md"):
        assert required in files, f"{required} missing from sveltekit scaffold"
    pkg = json.loads(files["package.json"])
    assert "@sveltejs/kit" in pkg["devDependencies"]
    assert "svelte" in pkg["devDependencies"]
    assert "<script lang=\"ts\">" in files["src/routes/+page.svelte"]
    assert "React" not in files["src/routes/+page.svelte"]


def test_react_ts_scaffold_is_typescript_first_shape():
    files = scaffold_for("react_ts", "demo", "a TypeScript React app")
    for required in ("package.json", "index.html", "vite.config.ts", "tsconfig.json", "src/main.tsx", "src/App.tsx", "README.md"):
        assert required in files, f"{required} missing from react_ts scaffold"
    pkg = json.loads(files["package.json"])
    assert "typescript" in pkg["devDependencies"]
    assert "@types/react" in pkg["devDependencies"]
    assert "src/main.jsx" not in files
    assert "export default function App()" in files["src/App.tsx"]


def test_new_web_frameworks_have_checklists_and_node_proof(tmp_path):
    for stack in ("vue", "sveltekit", "react_ts"):
        assert stack in _NODE_STACKS
        files = scaffold_for(stack, "demo", f"a {stack} app")
        root = tmp_path / stack
        _write(files, root)
        res = proof_run(root, checklist=file_checklist(stack), stack=stack)
        assert res.passed, res.to_dict()


def test_new_web_frameworks_resolve_in_skill_aliases():
    assert {"vue", "vuejs"} <= _stack_aliases("vue")
    assert {"sveltekit", "svelte"} <= _stack_aliases("sveltekit")
    assert {"react_ts", "typescript", "ts"} <= _stack_aliases("react_ts")
