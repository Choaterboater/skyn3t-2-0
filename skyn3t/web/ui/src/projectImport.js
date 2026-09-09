export function canImproveProject(project) {
  if (!project) return false;
  if (typeof project.can_improve === "boolean") return project.can_improve;
  return project.is_complete !== false;
}

export function importProjectBody({ path, slug = "", stack = "" }) {
  return {
    path: path.trim(),
    ...(slug.trim() ? { slug: slug.trim() } : {}),
    ...(stack.trim() ? { stack: stack.trim().toLowerCase() } : {}),
  };
}
