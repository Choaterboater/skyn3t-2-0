from __future__ import annotations

from pathlib import Path

import pytest

from skyn3t.agents.validate import validate_source


@pytest.mark.parametrize("extension", ["jsx", "tsx"])
@pytest.mark.parametrize("existing_project", [False, True])
@pytest.mark.parametrize(
    "source",
    [
        "const View = () => (<p>Don't hide the workflow.</p>);",
        'const View = () => (<p>A literal " is text.</p>);',
        "const View = () => (<p>A literal ` is text.</p>);",
        "const View = () => (<p>Status: [pending</p>);",
        "const View = () => (<p>Preview (optional</p>);",
        "const View = () => (<><p>Don't stop.</p><input title=\"Don't stop\" /></>);",
        (
            "const View = ({items}) => (<section>{items.map(item => "
            "(<p key={item.id}>{item.name}'s result</p>))}</section>);"
        ),
        "const View = ({active}) => (<p>{active ? <strong>It's ready.</strong> : 'Waiting'}</p>);",
        "const View = () => (<section>{ready && <p>Don't stop.</p>}</section>);",
        "const View = () => (<section>{ready || <p>It's waiting.</p>}</section>);",
        "const View = () => (<Panel right={<strong>It's ready.</strong>}>Don't stop.</Panel>);",
        "const View = () => (<Widget items={['first', 'second']}>It's ready.</Widget>);",
        "const View = () => (<p>(Don't hide this text.</p>);",
        "const View = () => (<p title=\"Look in here\">It's ready.</p>);",
        (
            "const View = () => (<Widget options={{label: \"Don't\"}}>"
            "{/* unmatched quote ' and bracket [ in a comment */}It's working.</Widget>);"
        ),
    ],
)
def test_valid_jsx_text_does_not_corrupt_javascript_delimiter_state(
    extension: str, existing_project: bool, source: str,
) -> None:
    ok, error = validate_source(
        f"src/View.{extension}",
        source,
        existing_project=existing_project,
        original=source if existing_project else "",
    )
    assert ok, error


@pytest.mark.parametrize("extension", ["js", "ts", "jsx", "tsx"])
@pytest.mark.parametrize(
    "source",
    [
        "function broken() { return 1;",
        "const values = [1, 2);",
        "const value = a > (b < c;",
        "const value = call({nested: [1, 2});",
    ],
)
def test_gross_javascript_delimiter_errors_still_fail(extension: str, source: str) -> None:
    ok, error = validate_source(f"src/broken.{extension}", source)
    assert not ok
    assert error


@pytest.mark.parametrize(
    "source",
    [
        "const View = () => (<p>{items.map(item => item}</p>);",
        "const View = () => (<p>{value)</p>);",
        "const View = () => (<Widget options={{values: [1, 2}} />);",
        "const View = () => (<p>Don't skip {call(}</p>);",
        "const View = () => (<p>Unexpected }</p>);",
    ],
)
def test_jsx_text_support_does_not_hide_broken_expressions(source: str) -> None:
    original = "const View = () => (<p>Don't hide a newly broken expression.</p>);"
    ok, error = validate_source(
        "src/View.jsx", source, existing_project=True, original=original,
    )
    assert not ok
    assert error


@pytest.mark.parametrize(
    "source",
    [
        "const identity = <T,>(value: T) => value;",
        "function Box<T>(props: {value: T}) { return <p>{String(props.value)}</p>; }",
        "const smaller = left < right; const larger = right > left;",
        "const text = \"<p>Don't parse quoted markup</p>\";",
    ],
)
def test_typescript_and_javascript_syntax_is_not_mistaken_for_jsx_text(source: str) -> None:
    ok, error = validate_source("src/View.tsx", source)
    assert ok, error


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("src/View.tsx", "const identity = <T,>(value: T => value;"),
        ("src/View.tsx", "const identity = <T extends unknown>(value: T => value;"),
        ("src/value.ts", "const value = <Thing>(input;"),
    ],
)
def test_typescript_angle_syntax_does_not_hide_broken_javascript(
    path: str, source: str,
) -> None:
    ok, error = validate_source(path, source)
    assert not ok
    assert error


@pytest.mark.parametrize("name", ["Studio", "Projects", "Workspace"])
def test_source_validation_accepts_existing_dashboard_workflows(name: str) -> None:
    root = Path(__file__).resolve().parents[1]
    path = root / "skyn3t" / "web" / "ui" / "src" / "routes" / f"{name}.jsx"
    source = path.read_text(encoding="utf-8")
    ok, error = validate_source(
        str(path), source, existing_project=True, original=source,
    )
    assert ok, error
