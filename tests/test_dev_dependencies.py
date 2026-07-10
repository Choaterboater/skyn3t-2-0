import importlib.util


def test_dev_extra_covers_test_and_live_web_imports() -> None:
    modules = (
        "PIL",
        "fastapi",
        "httpx",
        "jinja2",
        "playwright",
        "uvicorn",
        "websockets",
    )
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    assert not missing, f"install the complete test environment with .[dev]; missing: {missing}"
