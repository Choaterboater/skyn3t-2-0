function values(skill, key) {
  return Array.isArray(skill?.[key]) ? skill[key].map(String) : [];
}

export function filterSkills(skills, filters = {}) {
  const words = String(filters.search || "").toLowerCase().trim().split(/\s+/).filter(Boolean);
  return skills.filter((skill) => {
    if (filters.category && !values(skill, "categories").includes(filters.category)) return false;
    if (filters.topic && !values(skill, "topics").includes(filters.topic)) return false;
    if (filters.rating && Number(skill.content_audit?.rating) !== Number(filters.rating)) return false;
    if (filters.status === "active" && !skill.active) return false;
    if (filters.status === "quarantined" && !skill.quarantined) return false;
    if (filters.status === "review_required" && skill.admission_check?.status !== "review_required") return false;
    if (filters.status === "not_checked" && !["not_checked", "failed"].includes(skill.admission_check?.status || "not_checked")) return false;
    const chars = skill.content_audit?.advice_chars;
    const budget = skill.content_audit?.global_budget_chars ?? 6000;
    if (filters.budget === "fits" && !(chars <= budget)) return false;
    if (filters.budget === "oversized" && !(chars > budget)) return false;
    const text = [
      skill.name, skill.title, skill.slug, skill.description, skill.stack,
      ...values(skill, "categories"), ...values(skill, "topics"), ...values(skill, "tags"),
    ].join(" ").toLowerCase();
    return words.every((word) => text.includes(word));
  });
}

export function skillFacets(skills, key) {
  const counts = new Map();
  for (const skill of skills) {
    for (const value of new Set(values(skill, key))) counts.set(value, (counts.get(value) || 0) + 1);
  }
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

export function sortSkills(skills, sort = "rating") {
  return [...skills].sort((a, b) => {
    if (sort === "rating") {
      const difference = (b.content_audit?.rating ?? -1) - (a.content_audit?.rating ?? -1);
      if (difference) return difference;
    }
    if (sort === "size") {
      const difference = (a.content_audit?.advice_chars ?? Infinity) - (b.content_audit?.advice_chars ?? Infinity);
      if (difference) return difference;
    }
    return String(a.name || a.title || a.slug).localeCompare(String(b.name || b.title || b.slug));
  });
}
