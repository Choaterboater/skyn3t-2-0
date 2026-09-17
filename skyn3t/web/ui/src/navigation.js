export const NAV_GROUPS = [
  {
    label: "Work",
    items: [
      { to: "/overview", label: "Overview", description: "Home, status, and recent work", icon: "home" },
      { to: "/projects", label: "Projects", description: "Find, run, and ship projects", icon: "projects" },
      { to: "/studio", label: "Build", description: "Create software from a brief", icon: "build" },
      { to: "/workspace", label: "Workspace", description: "Preview and improve a project", icon: "workspace" },
    ],
  },
  {
    label: "Learning",
    items: [
      { to: "/agents", label: "Agents", description: "Agent availability and work", icon: "agents" },
      { to: "/cortex", label: "Cortex", description: "Review learning proposals", icon: "cortex" },
      { to: "/skills", label: "Skills", description: "Manage reusable capabilities", icon: "skills" },
      { to: "/brain", label: "Brain", description: "Explore learned knowledge", icon: "brain" },
    ],
  },
  {
    label: "System",
    items: [
      { to: "/activity", label: "Activity", description: "Inspect and replay events", icon: "activity" },
      { to: "/settings", label: "Settings", description: "Configure providers and runtime", icon: "settings" },
    ],
  },
];

export const NAV_ITEMS = NAV_GROUPS.flatMap((group) => group.items);

export function routeForPath(pathname) {
  return NAV_ITEMS.find((item) => item.to === pathname) || NAV_ITEMS[0];
}

export function filterPages(query, items = NAV_ITEMS) {
  const terms = String(query || "").trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return items;
  return items.filter((item) => {
    const haystack = `${item.label} ${item.navLabel || ""} ${item.description} ${item.to}`.toLowerCase();
    return terms.every((term) => haystack.includes(term));
  });
}

export function nextPageIndex(index, direction, length) {
  if (!length) return -1;
  return (index + direction + length) % length;
}

export const THEME_STORAGE_KEY = "skyn3t_theme";

export function validTheme(value) {
  return value === "light" || value === "dark" ? value : null;
}

export function preferredTheme(stored, prefersDark = false) {
  return validTheme(stored) || (prefersDark ? "dark" : "light");
}

export function readThemePreference(storage) {
  try {
    return { value: validTheme(storage?.getItem(THEME_STORAGE_KEY)), error: null };
  } catch (error) {
    return { value: null, error };
  }
}

export function writeThemePreference(storage, value) {
  try {
    storage?.setItem(THEME_STORAGE_KEY, validTheme(value));
    return { ok: true, error: null };
  } catch (error) {
    return { ok: false, error };
  }
}
