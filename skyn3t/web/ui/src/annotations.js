// ---------------------------------------------------------------------------
// Pin-collection state shaping for batched visual annotations (v0 Design Mode
// port). The preview bridge posts one element signature per click; these
// helpers turn that stream into numbered pins (selector + comment) that the
// workspace submits as ONE improve goal to
// POST /api/projects/{slug}/annotations/improve. Pure functions — the React
// pane in Workspace.jsx only renders and dispatches.
// ---------------------------------------------------------------------------

export const MAX_PINS = 20;

// Narrowest selector derivable from a bridge signature: "#id" wins, else a
// class chain. Mirrors the visual editor's selector_for_signature narrowing.
export function selectorForSignature(signature) {
  const id = String(signature?.element_id || signature?.id || "").trim();
  if (id) return `#${id}`;
  const raw = Array.isArray(signature?.classes)
    ? signature.classes
    : String(signature?.classes || "").split(/\s+/);
  const safe = raw
    .map((name) => String(name).trim())
    .filter(Boolean)
    .slice(0, 5);
  return safe.length ? `.${safe.join(".")}` : "";
}

// Short human label for a pin row, e.g. "h1 .hero".
export function elementLabel(signature) {
  const tag = String(signature?.tag || "").trim() || "element";
  const selector = selectorForSignature(signature);
  return selector ? `${tag} ${selector}` : tag;
}

function pinIdentity(signature) {
  return [
    String(signature?.tag || ""),
    selectorForSignature(signature),
    String(signature?.text || "").slice(0, 80),
  ].join("|");
}

// Append a pin for a clicked element. Re-clicking the element that owns the
// newest pin refreshes that pin instead of stacking a duplicate.
export function addPin(pins, signature) {
  if (!signature || typeof signature !== "object") return pins;
  if (pins.length >= MAX_PINS) return pins;
  const identity = pinIdentity(signature);
  const last = pins[pins.length - 1];
  if (last && last.identity === identity) {
    return [...pins.slice(0, -1), { ...last, signature }];
  }
  const id = pins.reduce((max, pin) => Math.max(max, pin.id), 0) + 1;
  return [
    ...pins,
    {
      id,
      identity,
      signature,
      selector: selectorForSignature(signature),
      comment: "",
    },
  ];
}

export function updatePinComment(pins, id, comment) {
  return pins.map((pin) => (pin.id === id ? { ...pin, comment } : pin));
}

export function removePin(pins, id) {
  return pins.filter((pin) => pin.id !== id);
}

// Every pin needs a comment — a numbered goal entry without one is noise.
export function canSubmit(pins) {
  return (
    pins.length >= 1 &&
    pins.length <= MAX_PINS &&
    pins.every((pin) => pin.comment.trim())
  );
}

// Request body for the annotations endpoint. No screenshots (html2canvas is
// unavailable); the signature carries the click-to-source evidence instead.
export function pinsToPayload(pins) {
  return {
    annotations: pins.map((pin) => ({
      selector: pin.selector,
      comment: pin.comment.trim(),
      signature: pin.signature,
    })),
  };
}
