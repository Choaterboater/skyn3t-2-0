"""'Build from a picture' — multimodal reference image input.

Covers the new ``LLMClient.complete(images=...)`` surface, the designer/architect
agents forwarding ``payload['reference_image']``, and the web endpoint accepting
a base64 data URL. EVERYTHING degrades: no image / non-vision backend (stub, CLI,
text-only) → exactly today's behavior, never a crash.
"""

from __future__ import annotations

import base64

import pytest

from skyn3t.adapters import llm as llm_mod
from skyn3t.adapters.llm import LLMClient, _to_data_url
from skyn3t.config.settings import Settings
from skyn3t.core.model_router import Tier

# A 1x1 transparent PNG (smallest valid).
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_DATA_URL = "data:image/png;base64," + base64.b64encode(_PNG_BYTES).decode("ascii")


def _client(backend: str, **kw) -> LLMClient:
    return LLMClient(Settings(llm_backend=backend, **kw))


# ---- path <-> data URL conversion ------------------------------------------
def test_path_image_converted_to_data_url(tmp_path):
    p = tmp_path / "ref.png"
    p.write_bytes(_PNG_BYTES)
    url = _to_data_url(str(p))
    assert url.startswith("data:image/png;base64,")
    # round-trips to the original bytes
    b64 = url.split(",", 1)[1]
    assert base64.b64decode(b64) == _PNG_BYTES


def test_data_url_passed_through_unchanged():
    assert _to_data_url(_DATA_URL) == _DATA_URL


# ---- openrouter multimodal request -----------------------------------------
class _Capture:
    """Capture the JSON body POSTed to OpenRouter."""

    def __init__(self):
        self.body = None

    def install(self, monkeypatch):
        captured = self

        class _Resp:
            def raise_for_status(self):  # noqa: D401
                return None

            def json(self):
                return {
                    "choices": [{"message": {"content": '{"ok": true}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }

        class _AsyncClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                captured.body = json
                return _Resp()

        monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _AsyncClient)


async def test_openrouter_image_builds_multimodal_content(monkeypatch):
    cap = _Capture()
    cap.install(monkeypatch)
    c = _client("openrouter", openrouter_api_key="sk-or-test",
                vision_model="openai/gpt-4o-mini")
    result = await c.complete("describe", tier=Tier.UI, images=[_DATA_URL])
    assert result.backend == "openrouter"

    body = cap.body
    # The user message content is now a list of parts (text + image_url).
    user_msg = body["messages"][-1]
    assert user_msg["role"] == "user"
    parts = user_msg["content"]
    assert isinstance(parts, list)
    kinds = {p["type"] for p in parts}
    assert kinds == {"text", "image_url"}
    img = next(p for p in parts if p["type"] == "image_url")
    assert img["image_url"]["url"] == _DATA_URL


async def test_openrouter_image_routes_to_vision_model(monkeypatch):
    cap = _Capture()
    cap.install(monkeypatch)
    c = _client("openrouter", openrouter_api_key="sk-or-test",
                vision_model="openai/gpt-4o-mini", free_only=False)
    await c.complete("describe", tier=Tier.STRONG, images=[_DATA_URL])
    # Tier.STRONG normally resolves to a non-vision deepseek model; with an image
    # we must route to the configured vision model.
    assert cap.body["model"] == "openai/gpt-4o-mini"


async def test_openrouter_image_path_converted(monkeypatch, tmp_path):
    cap = _Capture()
    cap.install(monkeypatch)
    p = tmp_path / "ref.png"
    p.write_bytes(_PNG_BYTES)
    c = _client("openrouter", openrouter_api_key="sk-or-test",
                vision_model="openai/gpt-4o-mini")
    await c.complete("describe", tier=Tier.UI, images=[str(p)])
    img = next(part for part in cap.body["messages"][-1]["content"]
               if part["type"] == "image_url")
    assert img["image_url"]["url"].startswith("data:image/png;base64,")


async def test_openrouter_image_keeps_vision_model_under_free_only(monkeypatch):
    # free_only is the default posture. A vision call still needs a vision model
    # (degrading to a free text-only model would silently drop the image), so the
    # configured vision model is kept rather than rewritten to a :free default.
    cap = _Capture()
    cap.install(monkeypatch)
    c = _client("openrouter", openrouter_api_key="sk-or-test",
                vision_model="openai/gpt-4o-mini", free_only=True)
    await c.complete("describe", tier=Tier.UI, images=[_DATA_URL])
    assert cap.body["model"] == "openai/gpt-4o-mini"


async def test_free_only_no_vision_model_drops_image(monkeypatch):
    # free_only (default) + NO vision_model configured: don't silently bill a paid
    # vision model — keep the free resolved model and send TEXT ONLY.
    cap = _Capture()
    cap.install(monkeypatch)
    c = _client("openrouter", openrouter_api_key="sk-or-test", free_only=True)
    await c.complete("describe", tier=Tier.UI, images=[_DATA_URL])
    content = cap.body["messages"][-1]["content"]
    assert isinstance(content, str) or all(p.get("type") != "image_url" for p in content)
    assert not cap.body["model"].startswith("openai/gpt-4o-mini")


async def test_free_only_off_uses_default_vision_model(monkeypatch):
    # Opted out of free_only + no vision_model: the paid default vision model is
    # used and the image IS sent.
    cap = _Capture()
    cap.install(monkeypatch)
    c = _client("openrouter", openrouter_api_key="sk-or-test", free_only=False)
    await c.complete("describe", tier=Tier.UI, images=[_DATA_URL])
    assert cap.body["model"] == "openai/gpt-4o-mini"
    content = cap.body["messages"][-1]["content"]
    assert any(p.get("type") == "image_url" for p in content)


async def test_openrouter_no_images_stays_text_only(monkeypatch):
    cap = _Capture()
    cap.install(monkeypatch)
    c = _client("openrouter", openrouter_api_key="sk-or-test")
    await c.complete("describe", tier=Tier.UI)
    # No image -> unchanged behavior: content is a plain string.
    assert isinstance(cap.body["messages"][-1]["content"], str)


# ---- degradation: stub / CLI ignore images, never crash --------------------
async def test_stub_backend_ignores_images():
    c = _client("stub")
    result = await c.complete("describe", tier=Tier.UI, images=[_DATA_URL])
    assert result.backend == "stub"
    assert result.text  # normal stub result, no crash


async def test_cli_backend_ignores_images(monkeypatch):
    monkeypatch.setattr(LLMClient, "_cli_cache", {}, raising=False)
    monkeypatch.setattr(llm_mod.shutil, "which", lambda b: f"/usr/bin/{b}")

    async def _boom(*_a, **_k):
        raise FileNotFoundError("cli missing")

    monkeypatch.setattr(llm_mod.asyncio, "create_subprocess_exec", _boom)
    # CLI with an image must not raise; it degrades to stub like today.
    result = await _client("claude_cli").complete("hi", tier=Tier.UI, images=[_DATA_URL])
    assert result.backend == "stub"


async def test_empty_images_list_is_text_only(monkeypatch):
    cap = _Capture()
    cap.install(monkeypatch)
    c = _client("openrouter", openrouter_api_key="sk-or-test")
    await c.complete("describe", tier=Tier.UI, images=[])
    assert isinstance(cap.body["messages"][-1]["content"], str)


# ---- designer / architect forward reference_image as images= ----------------
from skyn3t.adapters.llm import LLMResult  # noqa: E402
from skyn3t.agents.architect import ArchitectAgent  # noqa: E402
from skyn3t.agents.designer import DesignerAgent  # noqa: E402
from skyn3t.core.agent import TaskRequest  # noqa: E402
from skyn3t.core.events import EventBus  # noqa: E402


class _FakeLLM:
    """Capture the kwargs each ``complete`` call receives. Returns valid JSON so
    the agents take their real (non-stub) path."""

    backend = "openrouter"
    supports_image_input = True

    def __init__(self):
        self.calls: list[dict] = []

    async def complete(self, prompt, tier=None, **kwargs):
        self.calls.append({"prompt": prompt, "tier": tier, **kwargs})
        return LLMResult(
            text='{"stack": "react", "summary": "s", '
                 '"files": [{"path": "src/App.jsx", "purpose": "ui"}], '
                 '"theme": "dark", "palette": {"bg": "#000", "fg": "#fff", "accent": "#f00"}, '
                 '"typography": "sans", "layout": ["grid"], "components": ["header"]}',
            model="openai/gpt-4o-mini", backend="openrouter",
        )


async def test_designer_forwards_reference_image():
    # The DESIGNER is the visual consumer — it forwards the image (on a vision
    # backend). The architect deliberately does NOT (see below).
    bus = EventBus()
    fake = _FakeLLM()
    agent = DesignerAgent(event_bus=bus, llm=fake)
    await agent.start()
    result = await agent.run(TaskRequest(
        type="design",
        payload={"brief": "a dashboard", "slug": "dash", "reference_image": _DATA_URL},
    ))
    assert result.success
    assert fake.calls[0].get("images") == [_DATA_URL]


async def test_architect_does_not_attach_image():
    # Attaching an image would force the Tier.STRONG plan call onto a weaker
    # generic vision model — the architect keeps full strength, no images.
    bus = EventBus()
    fake = _FakeLLM()
    agent = ArchitectAgent(event_bus=bus, llm=fake)
    await agent.start()
    result = await agent.run(TaskRequest(
        type="architecture",
        payload={"brief": "a dashboard", "slug": "dash", "reference_image": _DATA_URL},
    ))
    assert result.success
    assert not fake.calls[0].get("images")


async def test_designer_omits_image_on_non_vision_backend():
    # A stub/CLI backend can't see images — the designer must not attach one nor
    # tell the model "an image is attached" (which it couldn't act on).
    class _StubFake(_FakeLLM):
        backend = "stub"
        supports_image_input = False

    bus = EventBus()
    fake = _StubFake()
    agent = DesignerAgent(event_bus=bus, llm=fake)
    await agent.start()
    await agent.run(TaskRequest(
        type="design",
        payload={"brief": "a dashboard", "slug": "dash", "reference_image": _DATA_URL},
    ))
    assert not fake.calls[0].get("images")
    assert "reference image is attached" not in fake.calls[0]["prompt"]


@pytest.mark.parametrize("agent_cls,task_type", [
    (DesignerAgent, "design"),
    (ArchitectAgent, "architecture"),
])
async def test_agent_omits_images_when_absent(agent_cls, task_type):
    bus = EventBus()
    fake = _FakeLLM()
    agent = agent_cls(event_bus=bus, llm=fake)
    await agent.start()
    result = await agent.run(TaskRequest(
        type=task_type, payload={"brief": "a dashboard", "slug": "dash"}))
    assert result.success
    # No reference image -> images must be omitted or empty (unchanged behavior).
    assert not fake.calls[0].get("images")


# ---- runner threads reference_image from extra into the stage payload -------
from unittest.mock import MagicMock  # noqa: E402

from skyn3t.studio.planner import BuildPlan  # noqa: E402
from skyn3t.studio.runner import StudioRunner  # noqa: E402


def _make_plan() -> BuildPlan:
    plan = BuildPlan.__new__(BuildPlan)
    object.__setattr__(plan, "slug", "dash")
    object.__setattr__(plan, "brief", "a dashboard")
    object.__setattr__(plan, "stack", "react")
    object.__setattr__(plan, "stages", [])
    object.__setattr__(plan, "checklist", ["src/App.jsx"])
    object.__setattr__(plan, "test_first", False)
    object.__setattr__(plan, "best_of_n", 1)
    object.__setattr__(plan, "notes", {})
    return plan


def test_base_payload_threads_reference_image():
    runner = StudioRunner(EventBus(), MagicMock())
    plan = _make_plan()
    payload = runner._base_payload(
        plan, "/proj", "/wt", {}, [], {"reference_image": _DATA_URL})
    assert payload.get("reference_image") == _DATA_URL


def test_base_payload_omits_reference_image_when_absent():
    runner = StudioRunner(EventBus(), MagicMock())
    plan = _make_plan()
    payload = runner._base_payload(plan, "/proj", "/wt", {}, [], None)
    assert "reference_image" not in payload


# ---- web endpoint: accept + save a base64 data URL --------------------------
from skyn3t.web import routes  # noqa: E402


class _FakeStudio:
    def __init__(self):
        self.extra = None

    async def start(self, brief, slug=None, extra=None):
        self.extra = extra
        return MagicMock()


class _FakeBus:
    async def emit(self, *a, **k):
        return None


class _FakeState:
    def __init__(self, studio):
        self.studio = studio
        self.event_bus = _FakeBus()
        self.builds = {}
        self._n = 0

    def new_build_id(self):
        self._n += 1
        return f"b{self._n}"


async def test_submit_build_decodes_and_passes_reference_image():
    studio = _FakeStudio()
    state = _FakeState(studio)
    res = await routes.submit_build(
        state, brief="a dashboard", reference_image=_DATA_URL)
    assert res["dispatched"] is True
    # Let the background task run.
    import asyncio
    await asyncio.sleep(0)
    extra = studio.extra or {}
    ref = extra.get("reference_image")
    assert ref, "reference_image not threaded into runner extra"
    # Saved to disk as a real file path the agents can read.
    if ref.startswith("data:"):
        assert ref == _DATA_URL
    else:
        with open(ref, "rb") as f:
            assert f.read() == _PNG_BYTES


async def test_submit_build_rejects_non_data_reference_image():
    # SECURITY: a bare filesystem path / remote URL in the API body must NOT be
    # threaded into the build — otherwise the server would read an arbitrary local
    # file (it gets base64-encoded and sent to the model) or fetch an SSRF target.
    import asyncio
    for bad in ("/etc/passwd", "file:///etc/passwd", "http://169.254.169.254/latest/"):
        studio = _FakeStudio()
        state = _FakeState(studio)
        await routes.submit_build(state, brief="a dashboard", reference_image=bad)
        await asyncio.sleep(0)
        assert not (studio.extra or {}).get("reference_image"), f"{bad!r} was threaded"


async def test_submit_build_without_image_unchanged():
    studio = _FakeStudio()
    state = _FakeState(studio)
    res = await routes.submit_build(state, brief="a dashboard")
    assert res["dispatched"] is True
    import asyncio
    await asyncio.sleep(0)
    assert "reference_image" not in (studio.extra or {})
