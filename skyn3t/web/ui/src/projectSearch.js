export function projectSearchText(project = {}) {
  const source = project.source && typeof project.source === "object"
    ? Object.values(project.source).join(" ")
    : "";
  return [project.slug, project.name, project.stack, project.status, project.verdict,
    project.delivery_state, source].filter(Boolean).join(" ").toLowerCase();
}

export function filterProjects(projects = [], query = "") {
  const terms = String(query).trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return projects;
  return projects.filter((project) => {
    const text = projectSearchText(project);
    return terms.every((term) => text.includes(term));
  });
}

export function projectsForSelector(projects = [], filteredProjects = [], selectedSlug = "") {
  if (!selectedSlug || filteredProjects.some((project) => project.slug === selectedSlug)) {
    return filteredProjects;
  }
  const selected = projects.find((project) => project.slug === selectedSlug);
  return selected ? [selected, ...filteredProjects] : filteredProjects;
}
