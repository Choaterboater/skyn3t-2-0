"""Deploy env-var manifest — every plan names the env vars the build reads.

The Ship pillar's plan must tell the operator which environment variables to
set before deploying: plan["env_vars"] is the sorted, deduplicated list and
the human-readable notes carry a "Required environment variables" section
(or an honest "none detected").
"""

from __future__ import annotations

from skyn3t.studio.deploy import plan_deploy


def _write(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def test_deploy_plan_lists_mixed_env_reads_sorted_and_deduped(tmp_path):
    _write(tmp_path, {
        "package.json": '{"scripts":{"start":"node server.js"}}',
        "server.js": (
            "const key = process.env.STRIPE_SECRET_KEY;\n"
            "const url = process.env.API_BASE_URL || '';\n"
            "const dup = process.env.STRIPE_SECRET_KEY;\n"
        ),
        "src/config.ts": "export const pub = import.meta.env.VITE_PUBLIC_KEY;\n",
        "main.py": (
            "import os\n"
            "token = os.getenv('BOT_TOKEN')\n"
            "region = os.environ['AWS_REGION']\n"
            "debug = os.environ.get('DEBUG_MODE')\n"
        ),
    })

    plan = plan_deploy(tmp_path, "node_express")

    assert plan.env_vars == [
        "API_BASE_URL", "AWS_REGION", "BOT_TOKEN", "DEBUG_MODE",
        "STRIPE_SECRET_KEY", "VITE_PUBLIC_KEY",
    ]
    assert plan.to_dict()["env_vars"] == plan.env_vars
    section = "Required environment variables: " + ", ".join(plan.env_vars)
    assert section in plan.notes


def test_deploy_plan_skips_vite_meta_vars_and_bundled_output(tmp_path):
    _write(tmp_path, {
        "index.html": "<h1>x</h1>",
        "src/app.ts": "if (import.meta.env.PROD) { console.log(import.meta.env.MODE); }\n",
        "dist/bundle.js": "const k = process.env.BUNDLED_BUILD_OUTPUT;\n",
        "node_modules/pkg/index.js": "const k = process.env.VENDOR_INTERNAL;\n",
    })

    plan = plan_deploy(tmp_path, "static")

    assert plan.env_vars == []
    assert "Required environment variables: none detected" in plan.notes


def test_deploy_plan_reports_none_when_no_env_reads(tmp_path):
    _write(tmp_path, {"index.html": "<h1>x</h1>", "styles.css": "body{}"})

    plan = plan_deploy(tmp_path, "static")

    assert plan.env_vars == []
    assert plan.to_dict()["env_vars"] == []
    assert "none detected" in plan.notes


def test_deploy_plan_env_manifest_never_breaks_planning(tmp_path):
    # A missing directory still yields a sane plan with an empty manifest.
    plan = plan_deploy(tmp_path / "nope", "fastapi")
    assert plan.env_vars == []
    assert plan.kind in {"container", "none"}
