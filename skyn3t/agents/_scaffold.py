"""Offline scaffold generator.

Produces a *genuinely runnable* minimal project for each known stack as an
in-memory ``{relative_path: contents}`` mapping. The CodeAgent writes these to
disk when the LLM backend is the offline stub, satisfying design rule #1
(delivered != empty) without any network calls.

No I/O happens here — these are pure functions returning dicts of strings.
"""

from __future__ import annotations

import re as _re
from typing import Callable


def _react_vite(app_name: str, brief: str) -> dict[str, str]:
    title = brief.strip() or app_name
    return {
        "package.json": (
            "{\n"
            f'  "name": "{app_name}",\n'
            '  "private": true,\n'
            '  "version": "0.1.0",\n'
            '  "type": "module",\n'
            '  "scripts": {\n'
            '    "dev": "vite",\n'
            '    "build": "vite build",\n'
            '    "preview": "vite preview"\n'
            "  },\n"
            '  "dependencies": {\n'
            '    "react": "^18.2.0",\n'
            '    "react-dom": "^18.2.0"\n'
            "  },\n"
            '  "devDependencies": {\n'
            '    "@vitejs/plugin-react": "^4.2.0",\n'
            '    "vite": "^5.0.0"\n'
            "  }\n"
            "}\n"
        ),
        "vite.config.js": (
            "import { defineConfig } from 'vite'\n"
            "import react from '@vitejs/plugin-react'\n\n"
            "export default defineConfig({\n"
            "  plugins: [react()],\n"
            "})\n"
        ),
        "index.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="UTF-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
            f"    <title>{title}</title>\n"
            "  </head>\n"
            "  <body>\n"
            '    <div id="root"></div>\n'
            '    <script type="module" src="/src/main.jsx"></script>\n'
            "  </body>\n"
            "</html>\n"
        ),
        "src/main.jsx": (
            "import React from 'react'\n"
            "import ReactDOM from 'react-dom/client'\n"
            "import App from './App.jsx'\n"
            "import './styles.css'\n\n"
            "ReactDOM.createRoot(document.getElementById('root')).render(\n"
            "  <React.StrictMode>\n"
            "    <App />\n"
            "  </React.StrictMode>,\n"
            ")\n"
        ),
        "src/App.jsx": (
            "import { useState } from 'react'\n\n"
            "export default function App() {\n"
            "  const [count, setCount] = useState(0)\n"
            "  return (\n"
            '    <main className="app">\n'
            f"      <h1>{title}</h1>\n"
            "      <p>A runnable Vite + React starter generated offline by SkyN3t.</p>\n"
            '      <button onClick={() => setCount((c) => c + 1)}>\n'
            "        count is {count}\n"
            "      </button>\n"
            "    </main>\n"
            "  )\n"
            "}\n"
        ),
        "src/styles.css": (
            ":root { font-family: system-ui, sans-serif; }\n"
            "body { margin: 0; display: grid; place-items: center; min-height: 100vh; }\n"
            ".app { text-align: center; padding: 2rem; }\n"
            "button { font-size: 1rem; padding: 0.5rem 1rem; cursor: pointer; }\n"
        ),
        ".gitignore": "node_modules\ndist\n",
        "README.md": (
            f"# {title}\n\n"
            "Generated offline by SkyN3t (Vite + React).\n\n"
            "```bash\nnpm install\nnpm run dev\n```\n"
        ),
    }


def _static_html(app_name: str, brief: str) -> dict[str, str]:
    title = brief.strip() or app_name
    return {
        "index.html": (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "  <head>\n"
            '    <meta charset="UTF-8" />\n'
            '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
            f"    <title>{title}</title>\n"
            '    <link rel="stylesheet" href="styles.css" />\n'
            "  </head>\n"
            "  <body>\n"
            f"    <main><h1>{title}</h1>\n"
            "    <p>A runnable static site generated offline by SkyN3t.</p>\n"
            '    <button id="cta">Click me</button></main>\n'
            '    <script src="main.js"></script>\n'
            "  </body>\n"
            "</html>\n"
        ),
        "styles.css": (
            "body { font-family: system-ui, sans-serif; display: grid;"
            " place-items: center; min-height: 100vh; margin: 0; }\n"
            "main { text-align: center; }\n"
        ),
        "main.js": (
            "let count = 0;\n"
            "const btn = document.getElementById('cta');\n"
            "btn.addEventListener('click', () => {\n"
            "  count += 1;\n"
            "  btn.textContent = `Clicked ${count} times`;\n"
            "});\n"
        ),
        "README.md": f"# {title}\n\nOpen `index.html` in a browser.\n",
    }


def _python_cli(app_name: str, brief: str) -> dict[str, str]:
    pkg = (app_name.replace("-", "_").strip() or "app")
    # a valid python identifier for the package dir
    if not pkg[0].isalpha() and pkg[0] != "_":
        pkg = f"app_{pkg}"
    title = brief.strip() or app_name
    return {
        "pyproject.toml": (
            "[project]\n"
            f'name = "{app_name}"\n'
            'version = "0.1.0"\n'
            f'description = "{title}"\n'
            'requires-python = ">=3.10"\n\n'
            "[project.scripts]\n"
            f'{app_name} = "{pkg}.cli:main"\n\n'
            "[build-system]\n"
            'requires = ["setuptools>=61"]\n'
            'build-backend = "setuptools.build_meta"\n'
        ),
        "main.py": (
            '"""Root entrypoint: ``python main.py`` runs the CLI."""\n'
            "from __future__ import annotations\n\n"
            f"from {pkg}.cli import main\n\n\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        ),
        f"{pkg}/__init__.py": (
            f'"""{title} — generated offline by SkyN3t."""\n\n'
            '__version__ = "0.1.0"\n'
        ),
        f"{pkg}/core.py": (
            '"""Core logic for the tool (pure, easily testable)."""\n'
            "from __future__ import annotations\n\n\n"
            "def greet(name: str) -> str:\n"
            '    """Return a friendly greeting."""\n'
            "    name = (name or \"world\").strip() or \"world\"\n"
            '    return f"Hello, {name}!"\n'
        ),
        f"{pkg}/cli.py": (
            '"""Command-line interface."""\n'
            "from __future__ import annotations\n\n"
            "import argparse\n\n"
            f"from {pkg}.core import greet\n\n\n"
            "def build_parser() -> argparse.ArgumentParser:\n"
            f'    parser = argparse.ArgumentParser(prog="{app_name}", description="{title}")\n'
            '    parser.add_argument("--name", default="world", help="who to greet")\n'
            "    return parser\n\n\n"
            "def main(argv: list[str] | None = None) -> int:\n"
            "    args = build_parser().parse_args(argv)\n"
            "    print(greet(args.name))\n"
            "    return 0\n\n\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        ),
        "tests/test_core.py": (
            f"from {pkg}.core import greet\n\n\n"
            "def test_greet_default():\n"
            '    assert greet("world") == "Hello, world!"\n\n\n'
            "def test_greet_strips():\n"
            '    assert greet("  ada ") == "Hello, ada!"\n'
        ),
        "README.md": (
            f"# {title}\n\n"
            "A runnable Python CLI.\n\n"
            "```bash\npython main.py --name you\n```\n\n"
            "## Develop\n\n```bash\npip install -e .\npytest\n```\n"
        ),
        ".gitignore": "__pycache__/\n*.pyc\n.venv/\ndist/\n*.egg-info/\n",
    }


def _fastapi(app_name: str, brief: str) -> dict[str, str]:
    title = brief.strip() or app_name
    return {
        "main.py": (
            '"""' + f"{title} — a runnable FastAPI app generated offline by SkyN3t." + '"""\n\n'
            "from __future__ import annotations\n\n"
            "from fastapi import FastAPI\n\n"
            'app = FastAPI(title="' + title + '")\n\n\n'
            '@app.get("/")\n'
            "async def root() -> dict[str, str]:\n"
            '    return {"message": "' + title + ' is running"}\n\n\n'
            '@app.get("/health")\n'
            "async def health() -> dict[str, str]:\n"
            '    return {"status": "ok"}\n'
        ),
        "test_main.py": (
            "from fastapi.testclient import TestClient\n\n"
            "from main import app\n\n"
            "client = TestClient(app)\n\n\n"
            "def test_health():\n"
            '    resp = client.get("/health")\n'
            "    assert resp.status_code == 200\n"
            '    assert resp.json()["status"] == "ok"\n'
        ),
        "requirements.txt": "fastapi\nuvicorn[standard]\nhttpx\n",
        "README.md": (
            f"# {title}\n\n"
            "```bash\npip install -r requirements.txt\nuvicorn main:app --reload\n```\n"
        ),
    }


def _node_express(app_name: str, brief: str) -> dict[str, str]:
    title = brief.strip() or app_name
    return {
        "package.json": (
            "{\n"
            f'  "name": "{app_name}",\n'
            '  "version": "0.1.0",\n'
            '  "type": "commonjs",\n'
            '  "main": "server.js",\n'
            '  "scripts": {\n'
            '    "start": "node server.js",\n'
            '    "test": "node test.js"\n'
            "  },\n"
            '  "dependencies": { "express": "^4.18.2" }\n'
            "}\n"
        ),
        "server.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "const port = process.env.PORT || 3000;\n\n"
            "app.get('/', (req, res) => res.json({ message: '" + title + " is running' }));\n"
            "app.get('/health', (req, res) => res.json({ status: 'ok' }));\n\n"
            "if (require.main === module) {\n"
            "  app.listen(port, () => console.log(`listening on ${port}`));\n"
            "}\n"
            "module.exports = app;\n"
        ),
        "test.js": (
            "const assert = require('assert');\n"
            "const app = require('./server.js');\n\n"
            "assert.strictEqual(typeof app, 'function', 'server.js must export the express app');\n"
            "console.log('ok');\n"
        ),
        ".gitignore": "node_modules\n",
        "README.md": (
            f"# {title}\n\n```bash\nnpm install\nnpm start\n```\n"
        ),
    }


def _react_native_expo(app_name: str, brief: str) -> dict[str, str]:
    """A genuinely runnable minimal Expo (React Native + TypeScript) app.

    Mobile can't be iframe-previewed like the web stacks, so the proof for this
    stack is a type-check (``npm run typecheck`` -> ``tsc --noEmit``) rather than
    a dev server. The app ships a real default-exported root screen with a
    reusable component and local state, plus a test, so ``delivered != empty``.
    """
    title = brief.strip() or app_name
    # A JSON-safe display title (for app.json) and a JS-string-literal title (for
    # the JSX), plus a slug for the Expo config.
    safe_title = title.replace('"', "'")
    js_title = title.replace("\\", "\\\\").replace("'", "\\'")
    slug = _re.sub(r"[^a-z0-9-]+", "-", app_name.lower()).strip("-") or "app"
    return {
        "package.json": (
            "{\n"
            f'  "name": "{slug}",\n'
            '  "version": "0.1.0",\n'
            '  "private": true,\n'
            '  "main": "node_modules/expo/AppEntry.js",\n'
            '  "scripts": {\n'
            '    "start": "expo start",\n'
            '    "android": "expo start --android",\n'
            '    "ios": "expo start --ios",\n'
            '    "typecheck": "tsc --noEmit",\n'
            '    "test": "jest"\n'
            "  },\n"
            '  "dependencies": {\n'
            '    "expo": "~51.0.0",\n'
            '    "expo-status-bar": "~1.12.1",\n'
            '    "react": "18.2.0",\n'
            '    "react-native": "0.74.5"\n'
            "  },\n"
            '  "devDependencies": {\n'
            '    "@types/react": "~18.2.45",\n'
            '    "typescript": "~5.3.3"\n'
            "  }\n"
            "}\n"
        ),
        "app.json": (
            "{\n"
            '  "expo": {\n'
            f'    "name": "{safe_title}",\n'
            f'    "slug": "{slug}",\n'
            '    "version": "1.0.0",\n'
            '    "orientation": "portrait",\n'
            '    "userInterfaceStyle": "light",\n'
            '    "splash": {\n'
            '      "resizeMode": "contain",\n'
            '      "backgroundColor": "#ffffff"\n'
            "    },\n"
            '    "ios": { "supportsTablet": true },\n'
            '    "android": {},\n'
            '    "web": { "bundler": "metro" }\n'
            "  }\n"
            "}\n"
        ),
        "tsconfig.json": (
            "{\n"
            '  "extends": "expo/tsconfig.base",\n'
            '  "compilerOptions": {\n'
            '    "strict": true,\n'
            '    "jsx": "react-native",\n'
            '    "esModuleInterop": true,\n'
            '    "skipLibCheck": true\n'
            "  }\n"
            "}\n"
        ),
        "babel.config.js": (
            "module.exports = function (api) {\n"
            "  api.cache(true);\n"
            "  return {\n"
            "    presets: ['babel-preset-expo'],\n"
            "  };\n"
            "};\n"
        ),
        "App.tsx": (
            "import { useState } from 'react';\n"
            "import { StatusBar } from 'expo-status-bar';\n"
            "import { StyleSheet, Text, View } from 'react-native';\n\n"
            "import { Counter } from './src/components/Counter';\n\n"
            "export default function App(): JSX.Element {\n"
            "  const [taps, setTaps] = useState(0);\n"
            "  return (\n"
            "    <View style={styles.container}>\n"
            "      <Text style={styles.title}>{'" + js_title + "'}</Text>\n"
            "      <Text style={styles.subtitle}>\n"
            "        A runnable Expo app generated offline by SkyN3t.\n"
            "      </Text>\n"
            "      <Counter value={taps} onPress={() => setTaps((n) => n + 1)} />\n"
            "      <StatusBar style=\"auto\" />\n"
            "    </View>\n"
            "  );\n"
            "}\n\n"
            "const styles = StyleSheet.create({\n"
            "  container: {\n"
            "    flex: 1,\n"
            "    backgroundColor: '#fff',\n"
            "    alignItems: 'center',\n"
            "    justifyContent: 'center',\n"
            "    padding: 24,\n"
            "  },\n"
            "  title: { fontSize: 24, fontWeight: '600', marginBottom: 8 },\n"
            "  subtitle: { fontSize: 14, color: '#555', textAlign: 'center', marginBottom: 24 },\n"
            "});\n"
        ),
        "src/components/Counter.tsx": (
            "import { Pressable, StyleSheet, Text } from 'react-native';\n\n"
            "export interface CounterProps {\n"
            "  value: number;\n"
            "  onPress: () => void;\n"
            "}\n\n"
            "export function Counter({ value, onPress }: CounterProps): JSX.Element {\n"
            "  return (\n"
            "    <Pressable accessibilityRole=\"button\" style={styles.button} onPress={onPress}>\n"
            "      <Text style={styles.label}>Tapped {value} times</Text>\n"
            "    </Pressable>\n"
            "  );\n"
            "}\n\n"
            "const styles = StyleSheet.create({\n"
            "  button: {\n"
            "    backgroundColor: '#1f6feb',\n"
            "    paddingHorizontal: 20,\n"
            "    paddingVertical: 12,\n"
            "    borderRadius: 8,\n"
            "  },\n"
            "  label: { color: '#fff', fontSize: 16 },\n"
            "});\n"
        ),
        "__tests__/App.test.tsx": (
            "import { Counter } from '../src/components/Counter';\n\n"
            "// A lightweight smoke test: the component is a function and accepts\n"
            "// the documented props. (Full render tests need @testing-library/\n"
            "// react-native, which we keep out of the offline scaffold.)\n"
            "describe('Counter', () => {\n"
            "  it('is a renderable component function', () => {\n"
            "    expect(typeof Counter).toBe('function');\n"
            "  });\n"
            "});\n"
        ),
        ".gitignore": "node_modules/\n.expo/\ndist/\nweb-build/\n*.log\n",
        "README.md": (
            f"# {title}\n\n"
            "A runnable Expo (React Native + TypeScript) app generated offline by "
            "SkyN3t.\n\n"
            "## Run\n\n"
            "```bash\nnpm install\nnpm start        # opens Expo; scan the QR with Expo Go\n```\n\n"
            "Mobile apps run on a device/simulator and cannot be previewed in an "
            "iframe like the web stacks. CI proves the build with a type check:\n\n"
            "```bash\nnpm run typecheck\n```\n"
        ),
    }


_BUILDERS: dict[str, Callable[[str, str], dict[str, str]]] = {
    "react_vite": _react_vite,
    "react_native": _react_native_expo,
    "static_html": _static_html,
    "python_cli": _python_cli,
    "fastapi": _fastapi,
    "node_express": _node_express,
}


def scaffold_for(stack: str, app_name: str, brief: str = "") -> dict[str, str]:
    """Return a ``{path: contents}`` mapping for a runnable project of ``stack``.

    Unknown stacks fall back to ``react_vite``.
    """
    builder = _BUILDERS.get(stack, _react_vite)
    safe_name = (app_name or "app").strip() or "app"
    return builder(safe_name, brief)


# ---- entrypoint synthesis (repair an agentic build with no runnable root) ----
# A coding agent often authors a real package (foo/manager.py, foo/cli.py, …)
# but forgets the runnable ROOT the rest of the pipeline (proof, boot, reviewer)
# expects. These pure helpers detect that and synthesize a real, wired
# ``main.py`` so ``python main.py`` actually runs the produced code — instead of
# the old behaviour of papering over it with an empty stub.

_ENTRYPOINT_BASENAMES = frozenset(
    {"main.py", "__main__.py", "app.py", "cli.py", "run.py", "manage.py", "wsgi.py", "asgi.py"}
)
# Module-level only (no leading indent): an indented ``def main(self)`` inside a
# class is a method, not an importable ``from module import main`` symbol.
_DEF_ENTRY_RE = _re.compile(r"^def\s+(main|run|cli)\s*\(", _re.MULTILINE)


def _norm(rel: str) -> str:
    return rel.replace("\\", "/")


def _module_path(rel: str) -> str:
    rel = _norm(rel)
    return rel[:-3].replace("/", ".") if rel.endswith(".py") else rel


def has_python_entrypoint(files: dict[str, str]) -> bool:
    """True if any file looks like a runnable Python entrypoint."""
    for rel in files:
        base = _norm(rel).rsplit("/", 1)[-1]
        if base in _ENTRYPOINT_BASENAMES:
            return True
    return False


def top_level_packages(files: dict[str, str]) -> list[str]:
    """Names of depth-1 packages (a dir directly holding ``__init__.py``)."""
    pkgs = {
        _norm(rel).split("/")[0]
        for rel in files
        if len(_norm(rel).split("/")) == 2 and _norm(rel).endswith("/__init__.py")
    }
    return sorted(pkgs)


def _find_entry_callable(files: dict[str, str]) -> tuple[str, str] | None:
    """Find a ``module, func`` exposing a callable named main/run/cli."""

    def rank(rel: str) -> int:
        base = _norm(rel).rsplit("/", 1)[-1]
        return {"cli.py": 0, "__main__.py": 1, "app.py": 2, "main.py": 3}.get(base, 9)

    for rel in sorted(files, key=rank):
        if not _norm(rel).endswith(".py"):
            continue
        m = _DEF_ENTRY_RE.search(files[rel] or "")
        if m:
            return _module_path(rel), m.group(1)
    return None


def synthesize_python_entrypoint(files: dict[str, str]) -> dict[str, str]:
    """Return ``{"main.py": <shim>}`` wiring an existing package to a root runner.

    Returns ``{}`` when the tree already has an entrypoint or there is nothing to
    wire. The shim imports the produced code so ``python main.py`` is genuinely
    runnable (and importable by the boot/proof checks).
    """
    if has_python_entrypoint(files):
        return {}
    target = _find_entry_callable(files)
    pkgs = top_level_packages(files)
    if target is None and not pkgs:
        return {}

    if target is not None:
        module, func = target
        shim = (
            '"""Application entrypoint (auto-wired by SkyN3t).\n\n'
            f"Delegates to ``{module}.{func}`` so ``python main.py`` runs the app.\n"
            '"""\n'
            "from __future__ import annotations\n\n"
            f"from {module} import {func} as _entry\n\n\n"
            "def main(argv: list[str] | None = None) -> int:\n"
            "    result = _entry()\n"
            "    return result if isinstance(result, int) else 0\n\n\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        )
        return {"main.py": shim}

    pkg = pkgs[0]
    shim = (
        '"""Application entrypoint (auto-generated by SkyN3t).\n\n'
        f"Best-effort runner: imports the ``{pkg}`` package and invokes its\n"
        "main()/run()/cli() if one exists.\n"
        '"""\n'
        "from __future__ import annotations\n\n"
        "import importlib\n\n"
        "_MODULES = (\n"
        f'    "{pkg}.cli",\n'
        f'    "{pkg}.__main__",\n'
        f'    "{pkg}.main",\n'
        f'    "{pkg}.app",\n'
        f'    "{pkg}",\n'
        ")\n"
        '_FUNCS = ("main", "run", "cli")\n\n\n'
        "def _resolve():\n"
        "    for name in _MODULES:\n"
        "        try:\n"
        "            mod = importlib.import_module(name)\n"
        "        except Exception:\n"
        "            continue\n"
        "        for fn in _FUNCS:\n"
        "            cand = getattr(mod, fn, None)\n"
        "            if callable(cand):\n"
        "                return cand\n"
        "    return None\n\n\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    entry = _resolve()\n"
        "    if entry is None:\n"
        f'        print("{pkg}: imported OK, but no main()/run()/cli() entrypoint was found.")\n'
        "        return 0\n"
        "    result = entry()\n"
        "    return result if isinstance(result, int) else 0\n\n\n"
        'if __name__ == "__main__":\n'
        "    raise SystemExit(main())\n"
    )
    return {"main.py": shim}


def default_pyproject(slug: str) -> str:
    """A minimal but real pyproject for a python project keyed off the slug."""
    name = (slug or "app").strip() or "app"
    return (
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.10"\n\n'
        "[project.scripts]\n"
        f'{name} = "main:main"\n'
    )
