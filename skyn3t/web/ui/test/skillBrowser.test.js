import test from "node:test";
import assert from "node:assert/strict";
import { filterSkills, skillFacets, sortSkills } from "../src/skillBrowser.js";

const skills = [
  { slug: "mcp", name: "MCP schemas", categories: ["MCP & tool servers"], topics: ["schemas"], active: true, content_audit: { rating: 4, advice_chars: 1800 } },
  { slug: "editor", name: "Desktop editor", categories: ["Desktop & mobile", "Frontend & UI"], topics: ["editor", "layout"], active: true, content_audit: { rating: 3, advice_chars: 2300 } },
  { slug: "blocked", name: "Old MCP layout", categories: ["MCP & tool servers"], topics: ["layout"], quarantined: true, content_audit: { rating: 1, advice_chars: 6300 } },
];

test("category and topic filters intersect and hide unrelated entries", () => {
  const result = filterSkills(skills, { category: "MCP & tool servers", topic: "layout" });
  assert.deepEqual(result.map(s => s.slug), ["blocked"]);
});

test("search matches category names and all words, case insensitively", () => {
  assert.deepEqual(filterSkills(skills, { search: "DESKTOP layout" }).map(s => s.slug), ["editor"]);
  assert.deepEqual(filterSkills(skills, { search: "does-not-exist" }), []);
});

test("facets describe only matching records, with no unrelated categories", () => {
  const matches = filterSkills(skills, { search: "mcp" });
  assert.deepEqual(skillFacets(matches, "categories"), [["MCP & tool servers", 2]]);
  assert.deepEqual(skillFacets(matches, "topics"), [["layout", 1], ["schemas", 1]]);
});

test("rating, status, and budget filters are independent of historical score", () => {
  assert.deepEqual(filterSkills(skills, { rating: "4", status: "active", budget: "fits" }).map(s => s.slug), ["mcp"]);
  assert.deepEqual(filterSkills(skills, { status: "quarantined" }).map(s => s.slug), ["blocked"]);
  assert.deepEqual(filterSkills(skills, { budget: "oversized" }).map(s => s.slug), ["blocked"]);
});

test("sorting never mutates query data and unreviewed skills sort last", () => {
  const rows = [...skills, { slug: "new", name: "New skill" }];
  assert.deepEqual(sortSkills(rows, "rating").map(s => s.slug), ["mcp", "editor", "blocked", "new"]);
  assert.deepEqual(rows.map(s => s.slug), ["mcp", "editor", "blocked", "new"]);
});

test("admission filters expose held and unchecked skills separately from activation", () => {
  const rows = [
    { slug: "held", active: false, admission_check: { status: "review_required" } },
    { slug: "good", active: true, admission_check: { status: "passed" } },
    { slug: "missing", active: false, admission_check: { status: "not_checked" } },
    { slug: "failed", active: false, admission_check: { status: "failed" } },
  ];
  assert.deepEqual(filterSkills(rows, { status: "review_required" }).map(s => s.slug), ["held"]);
  assert.deepEqual(filterSkills(rows, { status: "not_checked" }).map(s => s.slug), ["missing", "failed"]);
  assert.deepEqual(filterSkills(rows, { status: "active" }).map(s => s.slug), ["good"]);
});
