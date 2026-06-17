"""Offline scaffold generator.

Produces a *genuinely runnable* minimal project for each known stack as an
in-memory ``{relative_path: contents}`` mapping. The CodeAgent writes these to
disk when the LLM backend is the offline stub, satisfying design rule #1
(delivered != empty) without any network calls.

No I/O happens here — these are pure functions returning dicts of strings.
"""

from __future__ import annotations

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
    pkg = app_name.replace("-", "_") or "app"
    title = brief.strip() or app_name
    return {
        "main.py": (
            '"""' + f"{title} — a runnable CLI generated offline by SkyN3t." + '"""\n\n'
            "from __future__ import annotations\n\n"
            "import argparse\n\n\n"
            "def greet(name: str) -> str:\n"
            '    return f"Hello, {name}!"\n\n\n'
            "def main(argv: list[str] | None = None) -> int:\n"
            '    parser = argparse.ArgumentParser(description="' + title + '")\n'
            '    parser.add_argument("--name", default="world", help="who to greet")\n'
            "    args = parser.parse_args(argv)\n"
            "    print(greet(args.name))\n"
            "    return 0\n\n\n"
            'if __name__ == "__main__":\n'
            "    raise SystemExit(main())\n"
        ),
        "test_main.py": (
            "from main import greet\n\n\n"
            "def test_greet():\n"
            '    assert greet("world") == "Hello, world!"\n'
        ),
        "requirements.txt": "",
        "README.md": (
            f"# {title}\n\n"
            "```bash\npython main.py --name you\n```\n"
        ),
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
        ".gitignore": "node_modules\n",
        "README.md": (
            f"# {title}\n\n```bash\nnpm install\nnpm start\n```\n"
        ),
    }


_BUILDERS: dict[str, Callable[[str, str], dict[str, str]]] = {
    "react_vite": _react_vite,
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
