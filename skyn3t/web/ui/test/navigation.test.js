import test from "node:test";
import assert from "node:assert/strict";
import {
  NAV_ITEMS,
  filterPages,
  nextPageIndex,
  preferredTheme,
  readThemePreference,
  routeForPath,
  writeThemePreference,
} from "../src/navigation.js";

test("navigation contains every dashboard route once", () => {
  assert.equal(NAV_ITEMS.length, 10);
  assert.equal(new Set(NAV_ITEMS.map((item) => item.to)).size, 10);
  assert.equal(routeForPath("/studio").label, "Build");
});

test("page filtering searches labels, descriptions, and paths", () => {
  assert.deepEqual(filterPages("provider").map((item) => item.to), ["/settings"]);
  assert.deepEqual(filterPages("foundry").map((item) => item.to), ["/studio"]);
  assert.equal(filterPages("").length, 10);
});

test("palette selection wraps", () => {
  assert.equal(nextPageIndex(0, -1, 4), 3);
  assert.equal(nextPageIndex(3, 1, 4), 0);
  assert.equal(nextPageIndex(0, 1, 0), -1);
});

test("theme uses valid storage before OS preference", () => {
  assert.equal(preferredTheme("light", true), "light");
  assert.equal(preferredTheme("invalid", true), "dark");
  assert.equal(preferredTheme(null, false), "light");
});

test("theme storage failures stay local and explicit", () => {
  const blocked = {
    getItem() { throw new Error("blocked read"); },
    setItem() { throw new Error("blocked write"); },
  };
  assert.equal(readThemePreference(blocked).value, null);
  assert.match(readThemePreference(blocked).error.message, /blocked read/);
  assert.equal(writeThemePreference(blocked, "dark").ok, false);
  assert.match(writeThemePreference(blocked, "dark").error.message, /blocked write/);
});

test("an invalid stored theme remains an OS-derived preference", () => {
  let writes = 0;
  const storage = { getItem: () => "sepia", setItem: () => { writes += 1; } };
  const result = readThemePreference(storage);
  assert.deepEqual(result, { value: null, error: null });
  assert.equal(preferredTheme(result.value, true), "dark");
  assert.equal(writes, 0);
});
