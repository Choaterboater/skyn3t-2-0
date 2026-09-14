import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

test("the app brief is a multiline composer with its existing focus ref and label", () => {
  const studio = readFileSync(new URL("../src/routes/Studio.jsx", import.meta.url), "utf8");
  assert.ok(/<textarea\b[^>]*\bref=\{briefRef\}/.test(studio), "App brief must render a textarea");
  assert.ok(studio.includes('aria-label="App brief"'), "Preserve the app brief's accessible name");
});

test("Projects exposes and wires the shared project search", () => {
  const projects = readFileSync(new URL("../src/routes/Projects.jsx", import.meta.url), "utf8");
  assert.ok(projects.includes('aria-label="Search projects"'), "Render an accessible project search");
  assert.ok(/\bfilterProjects\s*\(/.test(projects), "Apply the shared project search to the collection");
});
