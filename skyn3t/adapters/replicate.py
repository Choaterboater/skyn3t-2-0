"""ReplicateClient — real image generation via Replicate, with hard degradation.

The build uses this to turn an image-implying brief (e.g. a kids coloring app's
animal line-art) into *real* generated assets instead of "crappy drawings". It
mirrors the discipline of :mod:`skyn3t.adapters.llm`:

* ``available`` reflects token presence — no token, no calls.
* ``generate_images`` POSTs a prediction, polls until terminal, and fetches the
  output image bytes. Every failure path (no token, HTTP error, timeout, a
  ``failed``/``canceled`` prediction, a malformed body, an unreadable output)
  **degrades to ``[]`` and never raises** — image-gen must never block or crash
  a build (design rule #6).
* It is bounded: a poll deadline + a per-request cap so a runaway model can't
  hang the build, and ``n`` is clamped so a brief can't ask for 1000 images.

``httpx`` is import-guarded even though it's already a dependency, so an unusual
environment degrades to "unavailable" rather than an ImportError at import time.
"""

from __future__ import annotations

import asyncio

import structlog

try:  # already a dependency, but guard so import never hard-fails the build
    import httpx
except Exception:  # noqa: BLE001 - degrade to "unavailable" rather than crash
    httpx = None  # type: ignore[assignment]

from skyn3t.config.settings import Settings, get_settings

log = structlog.get_logger(__name__)

# Fast, cheap, line-art-capable default. Overridable via settings.replicate_model
# (an "owner/name" official model id). flux-schnell is a few-step model so a
# coloring page returns in seconds for a few cents — the right default for the
# bounded, cost-aware asset step.
DEFAULT_MODEL = "black-forest-labs/flux-schnell"

_API_BASE = "https://api.replicate.com/v1"

# Coloring-book prompt template — bold clean outlines, no shading, white
# background. Keep it generic so any {subject} renders as a usable page.
_COLORING_TEMPLATE = (
    "black-and-white coloring-book page, bold clean black outlines, no shading, "
    "no gradients, no color, pure white background, simple cute friendly cartoon "
    "{subject}, centered, full body, thick lines suitable for a child to color"
)


def coloring_prompt(subject: str) -> str:
    """A coloring-page prompt for ``subject`` (e.g. 'elephant' -> a line-art page)."""
    return _COLORING_TEMPLATE.format(subject=(subject or "animal").strip())


# --- asset-style routing: different apps need different image models ---------
# Verified Replicate model ids (researched against replicate.com, 2026-06). The
# generic prompt-only input (_input_for) works across all of these, so the asset
# step just picks the right model + prompt template per app type.
_STYLE_MODELS = {
    "coloring":     DEFAULT_MODEL,                       # b&w line-art pages; fast + cheap
    "logo":         "recraft-ai/recraft-v4-svg",         # true scalable SVG vectors
    "sticker":      "fofr/sticker-maker",                # transparent-background stickers
    "photo":        "black-forest-labs/flux-1.1-pro",    # photorealism
    "illustration": "black-forest-labs/flux-dev",        # general colour art (default)
}

_STYLE_PROMPTS = {
    "coloring":     _COLORING_TEMPLATE,
    "logo":         ("minimal modern flat logo icon representing a {subject}, simple "
                     "geometric shapes, bold solid colors, clean vector, centered, plain "
                     "white background, no text, no lettering"),
    "sticker":      ("die-cut sticker of a cute {subject}, bold clean outline, vibrant "
                     "flat colors, glossy cartoon style, white border, transparent background"),
    "photo":        ("candid documentary-style high-resolution photograph of {subject}, "
                     "authentic real working environment, on the job, true to the trade, "
                     "natural light, sharp focus, photojournalistic, not posed, no text, "
                     "no watermark"),
    "illustration": ("colorful friendly flat vector illustration of a {subject}, clean "
                     "simple shapes, soft shadows, plain background, modern app asset, no text"),
}

# Brief signals -> style, in priority order (earlier wins on a tie). A generic
# image brief with none of these defaults to 'illustration'.
_STYLE_SIGNALS = (
    ("coloring", ("coloring", "colouring", "color-in", "color in", "line-art", "line art")),
    ("logo",     ("logo", "favicon", "app icon", "brand", "branding", "icon set",
                  "icons", "icon")),
    ("sticker",  ("sticker", "stickers", "emoji", "decal")),
    ("photo",    ("photo", "photograph", "photorealistic", "realistic", "product shot",
                  "product photo", "real-life", "recipe", "food", "menu", "restaurant",
                  "travel", "real estate", "portrait", "headshot",
                  # home-services / trades marketing sites want real photos, not vectors
                  "hvac", "plumbing", "electrical", "contractor", "heating",
                  "air conditioning")),
)


def select_asset_style(brief: str) -> str:
    """Pick the asset style (-> model + prompt) that best fits ``brief``.

    Defaults to 'illustration' for a generic image app. Keyword-based so it stays
    offline + deterministic; the matched style chooses BOTH the model and the
    prompt template, so a logo app gets a vector logo model, a photo app a
    photoreal model, etc. — not coloring-page line-art for everything."""
    low = (brief or "").lower()
    for style, sigs in _STYLE_SIGNALS:
        if any(s in low for s in sigs):
            return style
    return "illustration"


def asset_model(style: str) -> str:
    """Replicate model id for an asset ``style`` (falls back to the default)."""
    return _STYLE_MODELS.get(style, DEFAULT_MODEL)


def asset_prompt(style: str, subject: str) -> str:
    """Build the image prompt for ``subject`` in the given ``style``."""
    tmpl = _STYLE_PROMPTS.get(style, _STYLE_PROMPTS["illustration"])
    return tmpl.format(subject=(subject or "object").strip())


class ReplicateClient:
    """Minimal async Replicate client built from settings. Degrades to no-op."""

    # Bound the work so a stuck prediction can't hang a build.
    _poll_interval = 2.0
    _default_timeout = 90.0
    _max_images = 4

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.token = str(getattr(self.settings, "replicate_api_token", "") or "")
        self.model = (
            str(getattr(self.settings, "replicate_model", "") or "") or DEFAULT_MODEL
        )

    @property
    def available(self) -> bool:
        """True when a token is set AND httpx imported — the only state in which a
        real call can be made. Everything downstream checks this first."""
        return bool(self.token) and httpx is not None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def generate_images(
        self,
        prompt: str,
        n: int = 1,
        *,
        model: str | None = None,
        timeout: float | None = None,
    ) -> list[bytes]:
        """Generate up to ``n`` images for ``prompt``; return their raw bytes.

        Returns ``[]`` on no-token / any HTTP error / timeout / a non-succeeded
        prediction / a malformed or unreadable output — never raises. ``model``
        overrides the configured/default model for this call ("owner/name").
        """
        if not self.available:
            return []
        n = max(1, min(int(n), self._max_images))
        deadline = float(timeout or self._default_timeout)
        mdl = (model or self.model).strip()
        try:
            return await asyncio.wait_for(
                self._run(mdl, prompt, n), timeout=deadline + 5.0
            )
        except Exception as exc:  # noqa: BLE001 - incl. TimeoutError; never raise
            log.warning("replicate.generate_failed", model=mdl, error=str(exc)[:160])
            return []

    async def _run(self, model: str, prompt: str, n: int) -> list[bytes]:
        async with httpx.AsyncClient(timeout=self._default_timeout) as client:
            # One prediction per image (num_outputs isn't universal), but run them
            # CONCURRENTLY so total wall-clock is ~one prediction, not n sequential
            # ones — otherwise n>1 blows the outer wait_for budget and the build
            # stalls. Each _one_prediction/_fetch_image already never raises;
            # return_exceptions guards the rare propagation.
            preds = await asyncio.gather(
                *(self._one_prediction(client, model, prompt) for _ in range(n)),
                return_exceptions=True,
            )
            urls: list[str] = []
            for got in preds:
                if isinstance(got, list):
                    urls.extend(got)
            urls = urls[:n]
            fetched = await asyncio.gather(
                *(self._fetch_image(client, u) for u in urls),
                return_exceptions=True,
            )
            return [d for d in fetched if isinstance(d, (bytes, bytearray)) and d]

    async def _one_prediction(
        self, client: httpx.AsyncClient, model: str, prompt: str
    ) -> list[str]:
        """Create one prediction and poll it to a terminal state. Returns the
        list of output image URLs (possibly empty). Never raises."""
        create_url = f"{_API_BASE}/models/{model}/predictions"
        body = {"input": self._input_for(prompt, model)}
        # `Prefer: wait` lets Replicate hold the request open until the model
        # finishes (capped at 60s) — often the whole job in a single round-trip;
        # we still poll as a fallback when it returns early.
        headers = {**self._headers(), "Prefer": "wait"}
        resp = await client.post(create_url, json=body, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        pred_id = data.get("id") if isinstance(data, dict) else None
        status = str(data.get("status", "")) if isinstance(data, dict) else ""

        deadline = asyncio.get_event_loop().time() + self._default_timeout
        while status not in ("succeeded", "failed", "canceled"):
            if not pred_id or asyncio.get_event_loop().time() > deadline:
                return []
            await asyncio.sleep(self._poll_interval)
            poll = await client.get(
                f"{_API_BASE}/predictions/{pred_id}", headers=self._headers()
            )
            poll.raise_for_status()
            data = poll.json()
            status = str(data.get("status", "")) if isinstance(data, dict) else ""

        if status != "succeeded":
            log.warning("replicate.prediction_not_ok", status=status, id=pred_id)
            return []
        return self._output_urls(data.get("output"))

    def _input_for(self, prompt: str, model: str = "") -> dict:
        """Build a model's prediction input. ``prompt`` is the only universal field,
        so the default is minimal (works across SDXL/flux/recraft/coloring models).

        retro-diffusion/* are pixel game-sprite models with their OWN schema: send
        ``style='game_asset'`` for a game look, ``remove_bg=True`` for a transparent
        PNG in one call, and a small 256x256 size to stay in the cheap price tier.
        These keys are sent ONLY to retro-diffusion/* — flux/recraft reject them."""
        inp = {"prompt": prompt}
        if str(model).startswith("retro-diffusion/"):
            inp.update(style="game_asset", remove_bg=True, width=256, height=256)
        return inp

    @staticmethod
    def _output_urls(output) -> list[str]:
        """Normalize a prediction's ``output`` to a list of image URLs."""
        if isinstance(output, str):
            return [output] if output.startswith("http") else []
        if isinstance(output, list):
            return [u for u in output if isinstance(u, str) and u.startswith("http")]
        if isinstance(output, dict):
            # Some models nest under a key; take the first http value(s) we find.
            urls: list[str] = []
            for v in output.values():
                if isinstance(v, str) and v.startswith("http"):
                    urls.append(v)
                elif isinstance(v, list):
                    urls.extend(u for u in v if isinstance(u, str) and u.startswith("http"))
            return urls
        return []

    async def _fetch_image(self, client: httpx.AsyncClient, url: str) -> bytes | None:
        """Download one output image. Authorized (Replicate file URLs may need the
        token). Returns None on any failure — never raises."""
        try:
            resp = await client.get(url, headers={"Authorization": f"Bearer {self.token}"})
            resp.raise_for_status()
            return resp.content or None
        except Exception as exc:  # noqa: BLE001
            log.warning("replicate.fetch_failed", error=str(exc)[:160])
            return None
