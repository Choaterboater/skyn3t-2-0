import test from "node:test";
import assert from "node:assert/strict";
import { filterProjects, projectSearchText, projectsForSelector } from "../src/projectSearch.js";

const projects = [
  { slug: "client-portal", stack: "react", status: "completed" },
  { slug: "docs-api", stack: "python", verdict: "no_go", source: { kind: "local_import" } },
];

test("project search covers identity, stack, state, and source", () => {
  assert.deepEqual(filterProjects(projects, "react complete"), [projects[0]]);
  assert.deepEqual(filterProjects(projects, "local import"), [projects[1]]);
  assert.equal(filterProjects(projects, "missing").length, 0);
  assert.match(projectSearchText(projects[1]), /no_go/);
});

test("blank project search preserves the existing collection", () => {
  assert.equal(filterProjects(projects, "   "), projects);
});

test("workspace selector keeps the active project outside filtered results", () => {
  const matches = filterProjects(projects, "missing");
  assert.deepEqual(projectsForSelector(projects, matches, "client-portal"), [projects[0]]);
  assert.equal(projectsForSelector(projects, projects, "client-portal"), projects);
});
