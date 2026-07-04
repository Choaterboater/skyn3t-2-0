# Provider Routing Fix Report

## Summary

Fixed the CodeAgent CLI codegen branch so Studio/OpenRouter `model_override` pins are not passed through as CLI model names. When `codegen_cli_provider` is active, CLI agentic codegen now receives only `codegen_cli_model`; monolithic OpenRouter/global agentic codegen still receives `model_override`.

## RED Evidence

Command:

```bash
pytest -q tests/test_codegen_cli_routing.py::test_openrouter_model_override_does_not_replace_codegen_cli_model
```

Result before production change:

```text
FAILED tests/test_codegen_cli_routing.py::test_openrouter_model_override_does_not_replace_codegen_cli_model
AssertionError: assert 'openrouter/custom-selected' == 'sonnet'
```

## GREEN Evidence

Focused commands after the production change:

```bash
pytest -q tests/test_codegen_cli_routing.py::test_openrouter_model_override_does_not_replace_codegen_cli_model
pytest -q tests/test_codegen_cli_routing.py::test_codegen_cli_model_threaded_to_agentic_build
pytest -q tests/test_codegen_cli_routing.py::test_model_override_reaches_monolithic_agentic_build
```

Results:

```text
1 passed in 0.09s
1 passed in 0.09s
1 passed in 0.09s
```

Full routing test command:

```bash
pytest -q tests/test_codegen_cli_routing.py
```

Result:

```text
9 passed in 0.68s
```

## Files Changed

- `skyn3t/agents/code_agent.py`
- `tests/test_codegen_cli_routing.py`
- `.superpowers/sdd/provider-routing-fix-report.md`

## Self-Review

- Scope is limited to the assigned files.
- Production change is a one-line routing fix in the CLI agentic codegen call.
- Regression test covers the mixed case: CLI provider active, CLI model configured, OpenRouter manual override present.
- Existing monolithic OpenRouter behavior remains covered by `test_model_override_reaches_monolithic_agentic_build`.

## Skipped Tests

None. `test_codegen_cli_model_threaded_to_agentic_build` ran and passed in this environment.

## Residual Risk

Low. This only changes the model argument passed to the CLI codegen branch. If `codegen_cli_model` is empty, the CLI call receives `model=None`, preserving CLI default-model behavior without leaking OpenRouter model IDs.
