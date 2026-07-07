#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from skyn3t.studio.assessment_hygiene import check_assessment_artifacts


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    checks = check_assessment_artifacts(root)
    print(json.dumps({"ok": all(item["ok"] for item in checks), "checks": checks}, indent=2))
    return 0 if all(item["ok"] for item in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
