"""Install release wheels into clean environments and exercise installed behavior."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path

_SMOKE_PROGRAM = r"""
import json
import re
import sys
from importlib import metadata, resources
from pathlib import Path

import skyn3t
import skyn3t.web.app as web_app
from skyn3t.cli.main import app
from skyn3t.config.settings import Settings
from skyn3t.studio.golden_bench import load_suite

package_path = Path(skyn3t.__file__).resolve()
environment_path = Path(sys.prefix).resolve()
assert package_path.is_relative_to(environment_path), (
    f"imported skyn3t outside the clean environment: {package_path}"
)
assert metadata.version("skyn3t") == skyn3t.__version__
assert callable(app)

golden_path = resources.files("skyn3t.benchmarks").joinpath("golden-v1.json")
golden = json.loads(golden_path.read_text(encoding="utf-8"))
assert golden.get("schema_version") == 1
assert golden.get("cases"), "packaged golden suite has no cases"
assert len(load_suite().cases) == len(golden["cases"])

ui_root = resources.files("skyn3t.web").joinpath("ui").joinpath("dist")
index = ui_root.joinpath("index.html").read_text(encoding="utf-8")
assert web_app.UI_DIST_DIR.resolve() == Path(str(ui_root)).resolve()
assert ui_root.joinpath("THIRD_PARTY_NOTICES.txt").is_file()
assert ui_root.joinpath("fonts").joinpath("NOTICE.txt").is_file()
references = re.findall(r'(?:src|href)=["\']/(assets|fonts)/([^"\'?#]+)', index)
assert references, "dashboard index has no packaged asset references"
for directory, name in references:
    assert ui_root.joinpath(directory).joinpath(name).is_file(), (
        f"dashboard index references missing packaged file: /{directory}/{name}"
    )

runtime_root = Path.cwd() / "runtime"
settings = Settings(
    data_dir=runtime_root / "data",
    projects_dir=runtime_root / "projects",
    logs_dir=runtime_root / "logs",
    db_url=f"sqlite+aiosqlite:///{(runtime_root / 'runtime.db').as_posix()}",
    llm_backend="stub",
)
control_plane = web_app.create_app(settings=settings)
route_paths = {getattr(route, "path", None) for route in control_plane.routes}
for route in control_plane.routes:
    nested = getattr(route, "original_router", None)
    if nested is not None:
        route_paths.update(getattr(child, "path", None) for child in nested.routes)
assert "/api/status" in route_paths
"""


def _wheel_path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file() or path.suffix != ".whl":
        raise argparse.ArgumentTypeError(f"not a wheel file: {value}")
    return path


def _environment_commands(environment: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        scripts = environment / "Scripts"
        return scripts / "python.exe", scripts / "skyn3t.exe"
    scripts = environment / "bin"
    return scripts / "python", scripts / "skyn3t"


def smoke_wheel(wheel: Path, environment: Path, *, cwd: Path) -> None:
    """Install one wheel and prove it runs without access to the source checkout."""
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python, console_script = _environment_commands(environment)
    clean_env = os.environ.copy()
    clean_env.pop("PYTHONHOME", None)
    clean_env.pop("PYTHONPATH", None)
    clean_env["SKYN3T_LLM_BACKEND"] = "stub"
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            f"skyn3t[web] @ {wheel.as_uri()}",
        ],
        cwd=cwd,
        env=clean_env,
        check=True,
    )
    subprocess.run(
        [str(python), "-m", "pip", "check"],
        cwd=cwd,
        env=clean_env,
        check=True,
    )
    subprocess.run(
        [str(python), "-I", "-c", _SMOKE_PROGRAM],
        cwd=cwd,
        env=clean_env,
        check=True,
    )
    if not console_script.is_file():
        raise RuntimeError(f"wheel did not install the skyn3t console entry point: {console_script}")
    subprocess.run(
        [str(console_script), "--help"],
        cwd=cwd,
        env=clean_env,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheels", nargs="+", type=_wheel_path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="skyn3t-release-smoke-") as temporary:
        root = Path(temporary).resolve()
        for index, wheel in enumerate(args.wheels, start=1):
            environment = root / f"environment-{index}"
            smoke_wheel(wheel, environment, cwd=root)
            print(f"PASS clean installed-wheel smoke: {wheel.name}")


if __name__ == "__main__":
    main()
