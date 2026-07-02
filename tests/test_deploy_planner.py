"""Deploy planner — the 'Ship' pillar's first slice.

skyn3t builds + proves + previews LOCALLY (app_runner is localhost-only). This
turns a proven build into a concrete, keyless plan to put it on the internet:
classify the build → name the right hosts → emit the exact one-command deploy,
and for server stacks emit a ready Dockerfile. Keyless dry-run (no token, no
network): every stack a skyn3t build can produce maps to an honest deploy path
or an honest 'not a hosted app' verdict. Real token-gated deploy is a later
slice; this is the foundation and it's fully offline-provable.
"""

from __future__ import annotations

from skyn3t.studio.deploy import DEPLOY_KIND, plan_deploy, write_deploy_artifacts


def _write(tmp_path, files):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp_path


def test_every_builder_stack_has_a_deploy_kind():
    # Registry completeness: no stack should be silently un-shippable. A stack
    # with no hosted target is explicitly "artifact"/"mobile", not missing.
    from skyn3t.agents._common import KNOWN_STACKS

    for stack in KNOWN_STACKS:
        assert stack in DEPLOY_KIND, f"{stack} has no deploy kind"
        assert DEPLOY_KIND[stack] in {"static", "node_ssr", "container", "artifact", "mobile"}


def test_static_web_build_plans_a_static_host(tmp_path):
    _write(tmp_path, {"index.html": "<h1>hi</h1>", "styles.css": "body{}"})
    plan = plan_deploy(tmp_path, "static")
    assert plan.deployable and plan.kind == "static"
    assert "cloudflare-pages" in plan.targets
    assert "wrangler pages deploy" in plan.command
    assert plan.artifacts == {}  # static needs no Dockerfile


def test_vite_react_build_deploys_the_dist_output(tmp_path):
    _write(tmp_path, {"package.json": '{"scripts":{"build":"vite build"}}', "src/main.jsx": "x"})
    plan = plan_deploy(tmp_path, "react")
    assert plan.kind == "static"
    assert plan.output_dir == "dist", "a vite build ships dist/, not the source tree"
    assert "npm run build" in plan.build_command


def test_fastapi_server_plans_a_container_with_a_dockerfile(tmp_path):
    _write(tmp_path, {"main.py": "from fastapi import FastAPI\napp=FastAPI()", "requirements.txt": "fastapi\nuvicorn"})
    plan = plan_deploy(tmp_path, "fastapi")
    assert plan.deployable and plan.kind == "container"
    assert "fly" in plan.targets and "railway" in plan.targets
    dockerfile = plan.artifacts.get("Dockerfile", "")
    assert "python:" in dockerfile
    assert "requirements.txt" in dockerfile
    assert "PORT" in dockerfile, "the container must bind the platform's $PORT"
    assert 'CMD ["python", "main.py"]' in dockerfile


def test_rag_and_workflow_servers_are_containers(tmp_path):
    for stack in ("rag", "workflow"):
        root = tmp_path / stack
        _write(root, {"main.py": "from fastapi import FastAPI\napp=FastAPI()", "requirements.txt": "fastapi"})
        assert plan_deploy(root, stack).kind == "container"


def test_nextjs_is_ssr_not_a_static_drop(tmp_path):
    _write(tmp_path, {"package.json": '{"scripts":{"build":"next build","start":"next start"}}'})
    plan = plan_deploy(tmp_path, "nextjs")
    assert plan.kind == "node_ssr"
    assert "vercel" in plan.targets


def test_cli_and_pack_are_honest_artifacts_not_urls(tmp_path):
    for stack in ("python_cli", "agent_pack", "mcp"):
        root = tmp_path / stack
        _write(root, {"README.md": "x", "main.py": "print(1)"})
        plan = plan_deploy(root, stack)
        assert plan.kind == "artifact"
        # Honest: not a hosted URL — a publishable/runnable artifact.
        assert not plan.serves_url
        assert plan.command  # still emits how to distribute it


def test_write_deploy_artifacts_emits_the_dockerfile(tmp_path):
    _write(tmp_path, {"main.py": "x", "requirements.txt": "fastapi"})
    plan = plan_deploy(tmp_path, "fastapi")
    written = write_deploy_artifacts(plan, tmp_path)
    assert (tmp_path / "Dockerfile").exists()
    assert "Dockerfile" in written
    # Idempotent + non-destructive: never clobbers a user-authored Dockerfile.
    (tmp_path / "Dockerfile").write_text("# mine\n")
    write_deploy_artifacts(plan, tmp_path)
    assert (tmp_path / "Dockerfile").read_text() == "# mine\n"


def test_target_selects_the_host_specific_command(tmp_path):
    # --target is honored: the requested host moves to the front AND its own
    # deploy command is chosen (not a silently-ignored no-op).
    _write(tmp_path, {"index.html": "<h1>x</h1>"})
    default = plan_deploy(tmp_path, "static")
    picked = plan_deploy(tmp_path, "static", target="netlify")
    assert default.to_dict() != picked.to_dict(), "target must change the plan"
    assert picked.targets[0] == "netlify"
    assert "netlify deploy" in picked.command
    assert "wrangler" not in picked.command  # the default command was replaced


def test_unavailable_target_keeps_default_and_explains(tmp_path):
    # A container can't deploy to vercel — keep the default host and say why.
    _write(tmp_path, {"main.py": "x", "requirements.txt": "fastapi"})
    plan = plan_deploy(tmp_path, "fastapi", target="vercel")
    assert plan.targets[0] == "fly"          # default host kept
    assert "fly launch" in plan.command
    assert "vercel" in plan.notes            # the ignored preference is explained


def test_content_detection_classifies_nextjs_as_ssr_not_static(tmp_path):
    # No stack passed → the content-detection fallback. A modern Next app uses
    # next.config.mjs / a `next` dependency; the old next.config.js-only check
    # misrouted such apps to a static host (breaking SSR/API routes).
    _write(tmp_path, {
        "package.json": '{"dependencies":{"next":"14.0.0"},'
                        '"scripts":{"dev":"next dev","build":"next build","start":"next start"}}',
        "next.config.mjs": "export default {}",
        "app/page.jsx": "export default function Page(){return null}",
    })
    plan = plan_deploy(tmp_path, "")  # empty stack → content-detect
    assert plan.kind == "node_ssr", "a Next.js app must not be planned as a static drop"
    assert plan.serves_url


def test_plan_is_json_serializable(tmp_path):
    _write(tmp_path, {"index.html": "<h1>x</h1>"})
    d = plan_deploy(tmp_path, "static").to_dict()
    assert d["kind"] == "static" and isinstance(d["targets"], list)
    assert set(d) >= {"deployable", "kind", "targets", "command", "build_command", "output_dir", "artifacts", "serves_url", "notes"}


def test_planner_never_raises_on_garbage(tmp_path):
    plan = plan_deploy(tmp_path / "nope", "fastapi")
    assert plan.kind in {"container", "none"}  # missing dir → still a sane plan
    assert plan_deploy(tmp_path, "").kind  # empty stack → content-detected or none
