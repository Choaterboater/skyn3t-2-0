import test from "node:test";
import assert from "node:assert/strict";

import {
  addPin,
  canSubmit,
  elementLabel,
  pinsToPayload,
  removePin,
  selectorForSignature,
  updatePinComment,
  MAX_PINS,
} from "../src/annotations.js";

const heroClick = {
  tag: "h1",
  element_id: "",
  classes: ["hero", "big"],
  text: "Old title",
  image_src: "",
};
const ledeClick = {
  tag: "p",
  element_id: "lede",
  classes: [],
  text: "intro copy",
  image_src: "",
};

test("selectorForSignature prefers the element id, then a class chain", () => {
  assert.equal(selectorForSignature(ledeClick), "#lede");
  assert.equal(selectorForSignature(heroClick), ".hero.big");
  assert.equal(selectorForSignature({ tag: "div" }), "");
  assert.equal(elementLabel(heroClick), "h1 .hero.big");
  assert.equal(elementLabel({ tag: "div" }), "div");
});

test("addPin numbers pins and re-clicking the same element refreshes", () => {
  let pins = [];
  pins = addPin(pins, heroClick);
  pins = addPin(pins, ledeClick);
  assert.equal(pins.length, 2);
  assert.deepEqual(
    pins.map((pin) => pin.id),
    [1, 2],
  );
  // Re-click of the newest pin's element: refresh, no duplicate.
  pins = addPin(pins, { ...ledeClick, text: "intro copy" });
  assert.equal(pins.length, 2);
  // A different element after that appends a new pin.
  pins = addPin(pins, heroClick);
  assert.equal(pins.length, 3);
  assert.equal(pins[2].id, 3);
});

test("addPin is bounded by MAX_PINS", () => {
  let pins = [];
  for (let i = 0; i < MAX_PINS + 5; i += 1) {
    pins = addPin(pins, { ...heroClick, text: `title ${i}` });
  }
  assert.equal(pins.length, MAX_PINS);
});

test("comment editing, deletion, and submit gating", () => {
  let pins = addPin(addPin([], heroClick), ledeClick);
  assert.equal(canSubmit(pins), false); // comments still empty
  pins = updatePinComment(pins, 1, "bigger headline");
  assert.equal(canSubmit(pins), false);
  pins = updatePinComment(pins, 2, "  pad me  ");
  assert.equal(canSubmit(pins), true);

  pins = removePin(pins, 1);
  assert.equal(pins.length, 1);
  assert.equal(pins[0].id, 2);
  // Ids stay monotonic after deletion — no reuse of id 1.
  pins = addPin(pins, heroClick);
  assert.equal(pins[1].id, 3);
});

test("pinsToPayload shapes the annotations request body", () => {
  let pins = addPin(addPin([], heroClick), ledeClick);
  pins = updatePinComment(pins, 1, "bigger headline");
  pins = updatePinComment(pins, 2, "  pad me  ");
  const payload = pinsToPayload(pins);
  assert.deepEqual(
    payload.annotations.map((a) => [a.selector, a.comment]),
    [
      [".hero.big", "bigger headline"],
      ["#lede", "pad me"],
    ],
  );
  assert.equal(payload.annotations[0].signature.tag, "h1");
  assert.equal("screenshot_b64" in payload.annotations[0], false);
});
