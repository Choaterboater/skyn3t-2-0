import React, { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Navigate, NavLink, Route, Routes, useLocation, useNavigate } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { queryFn, useEventStream } from "./api.js";
import Icon from "./components/Icons.jsx";
import PendingApprovalsBanner from "./components/PendingApprovalsBanner.jsx";
import { NAV_GROUPS, filterPages, nextPageIndex, preferredTheme, readThemePreference, routeForPath, writeThemePreference } from "./navigation.js";

const Overview = lazy(() => import("./routes/Overview.jsx"));
const Agents = lazy(() => import("./routes/Agents.jsx"));
const Studio = lazy(() => import("./routes/Studio.jsx"));
const Cortex = lazy(() => import("./routes/Cortex.jsx"));
const Brain = lazy(() => import("./routes/Brain.jsx"));
const Skills = lazy(() => import("./routes/Skills.jsx"));
const Activity = lazy(() => import("./routes/Activity.jsx"));
const Settings = lazy(() => import("./routes/Settings.jsx"));
const Projects = lazy(() => import("./routes/Projects.jsx"));
const Workspace = lazy(() => import("./routes/Workspace.jsx"));

const FOCUSABLE = "a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex='-1'])";

function useDesktopLayout() {
  const query = "(min-width: 1024px)";
  const [desktop, setDesktop] = useState(() => typeof matchMedia === "undefined" || matchMedia(query).matches);
  useEffect(() => {
    const media = matchMedia(query);
    const update = () => setDesktop(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  return desktop;
}

function useModalFocus(open, dialogRef, onClose, initialFocusRef, restoreRef, fallbackRestoreRef) {
  useEffect(() => {
    if (!open) return undefined;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    requestAnimationFrame(() => (initialFocusRef?.current || dialogRef.current)?.focus());
    const keydown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const nodes = [...(dialogRef.current?.querySelectorAll(FOCUSABLE) || [])]
        .filter((node) => !node.hidden && node.getClientRects().length > 0);
      if (!nodes.length) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      const first = nodes[0];
      const last = nodes.at(-1);
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      document.body.style.overflow = previousOverflow;
      requestAnimationFrame(() => {
        const target = restoreRef?.current;
        const usable = target?.isConnected && !target.disabled && !target.closest?.("[inert]");
        (usable ? target : fallbackRestoreRef?.current)?.focus?.();
      });
    };
  }, [dialogRef, fallbackRestoreRef, initialFocusRef, onClose, open, restoreRef]);
}

function StaleCodeBanner() {
  const health = useQuery({ queryKey: ["health-stale"], queryFn: queryFn("/health"), refetchInterval: 60_000, refetchOnWindowFocus: true });
  if (!health.data?.stale_code) return null;
  const started = health.data.started_at ? new Date(health.data.started_at * 1000).toLocaleString() : "unknown";
  return (
    <div role="alert" className="notice notice-warning mb-5">
      This server is running <strong>stale code</strong>. Source changed after it started ({started}). Restart with <code>skyn3t start --web</code> before trusting build results.
    </div>
  );
}

class ViewErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <section role="alert" className="notice notice-error p-5">
        <h1 className="text-xl font-semibold text-bone">This view could not load</h1>
        <p className="mt-2 text-sm">Reload the dashboard to retry. Other pages remain available.</p>
        <pre className="mt-3 max-h-32 overflow-auto whitespace-pre-wrap text-xs">{String(this.state.error?.message || this.state.error)}</pre>
        <button type="button" className="btn-ember mt-4" onClick={() => window.location.reload()}>Reload dashboard</button>
      </section>
    );
  }
}

const WS = {
  open: { label: "Connected", cls: "status-good" },
  connecting: { label: "Connecting", cls: "status-warn" },
  closed: { label: "Offline", cls: "status-muted" },
  error: { label: "Connection error", cls: "status-bad" },
};

function Sidebar({ blocked = false, modal, onClose, open, restoreRef, stream }) {
  const dialogRef = useRef(null);
  const firstLinkRef = useRef(null);
  useModalFocus(modal && open, dialogRef, onClose, firstLinkRef, restoreRef);
  const ws = WS[stream.status] || WS.closed;
  if (modal && !open) return null;
  return (
    <>
      {modal && open ? <button className="drawer-overlay" aria-label="Close navigation" onClick={onClose} /> : null}
      <aside
        ref={dialogRef}
        className={`app-sidebar ${open ? "is-open" : ""}`}
        aria-labelledby={modal ? "mobile-navigation-title" : undefined}
        aria-label={modal ? undefined : "Application sidebar"}
        aria-modal={modal ? "true" : undefined}
        role={modal ? "dialog" : undefined}
        tabIndex={modal ? -1 : undefined}
        inert={blocked ? true : undefined}
        aria-hidden={blocked ? "true" : undefined}
      >
        <div className="flex min-h-16 items-center justify-between px-5">
          <NavLink ref={firstLinkRef} id={modal ? "mobile-navigation-title" : undefined} to="/overview" className="wordmark" onClick={onClose}>
            SKY<span>N3T</span>
          </NavLink>
          {modal ? <button className="icon-button" onClick={onClose} aria-label="Close navigation"><Icon name="close" /></button> : null}
        </div>
        <nav className="sidebar-nav" aria-label="Primary navigation">
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="nav-group">
              <p>{group.label}</p>
              {group.items.map((item) => (
                <NavLink key={item.to} to={item.to} onClick={onClose} className={({ isActive }) => `nav-link ${isActive ? "nav-link-active" : ""}`}>
                  <Icon name={item.icon} /><span>{item.navLabel || item.label}</span>
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-status" role="status" aria-live="polite">
          <span className={`status-dot ${ws.cls}`} /><span>{ws.label}</span>
          <span className="ml-auto font-mono text-xs">{stream.events?.length || 0} events</span>
        </div>
      </aside>
    </>
  );
}

function CommandPalette({ fallbackRestoreRef, onClose, open, restoreRef }) {
  const navigate = useNavigate();
  const inputRef = useRef(null);
  const dialogRef = useRef(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const results = useMemo(() => filterPages(query), [query]);
  useEffect(() => { setActive(0); }, [query]);
  useEffect(() => { if (open) setQuery(""); }, [open]);
  useModalFocus(open, dialogRef, onClose, inputRef, restoreRef, fallbackRestoreRef);
  if (!open) return null;
  const choose = (item) => { navigate(item.to); onClose(); };
  return (
    <div className="dialog-layer" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={dialogRef} className="command-dialog" role="dialog" aria-modal="true" aria-labelledby="palette-title" tabIndex={-1}>
        <h2 id="palette-title" className="sr-only">Jump to a page</h2>
        <div className="command-input">
          <Icon name="search" />
          <label className="sr-only" htmlFor="page-search">Jump to a page</label>
          <input
            id="page-search"
            ref={inputRef}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded="true"
            aria-controls="page-results"
            aria-activedescendant={results[active] ? `page-option-${active}` : undefined}
            value={query}
            placeholder="Find a page…"
            autoComplete="off"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") { event.preventDefault(); setActive((index) => nextPageIndex(index, 1, results.length)); }
              if (event.key === "ArrowUp") { event.preventDefault(); setActive((index) => nextPageIndex(index, -1, results.length)); }
              if (event.key === "Enter" && results[active]) { event.preventDefault(); choose(results[active]); }
            }}
          />
          <kbd>Esc</kbd>
        </div>
        <div id="page-results" className="command-results" role="listbox" aria-label="Pages">
          {results.length ? results.map((item, index) => (
            <button id={`page-option-${index}`} key={item.to} role="option" aria-selected={index === active} className={index === active ? "is-active" : ""} onMouseEnter={() => setActive(index)} onClick={() => choose(item)}>
              <span className="command-icon"><Icon name={item.icon} /></span>
              <span><strong>{item.label}</strong><small>{item.description}</small></span>
              <span className="command-path">{item.to}</span>
            </button>
          )) : <p className="command-empty">No pages match “{query}”.</p>}
        </div>
        <p className="command-help">Use ↑ and ↓ to move, Enter to open</p>
      </div>
    </div>
  );
}

export default function App() {
  const stream = useEventStream();
  const health = useQuery({ queryKey: ["health"], queryFn: queryFn("/health") });
  const location = useLocation();
  const route = routeForPath(location.pathname);
  const desktop = useDesktopLayout();
  const menuButtonRef = useRef(null);
  const searchButtonRef = useRef(null);
  const paletteRestoreRef = useRef(null);
  const [drawer, setDrawer] = useState(false);
  const [palette, setPalette] = useState(false);
  const initialPreference = useMemo(() => readThemePreference(typeof localStorage === "undefined" ? null : localStorage), []);
  const [themeOverride, setThemeOverride] = useState(initialPreference.value);
  const [systemDark, setSystemDark] = useState(() => typeof matchMedia !== "undefined" && matchMedia("(prefers-color-scheme: dark)").matches);
  const [themeWarning, setThemeWarning] = useState(initialPreference.error ? "Theme preference storage is unavailable; your selection may not persist." : "");
  const theme = preferredTheme(themeOverride, systemDark);
  const closeDrawer = useCallback(() => setDrawer(false), []);
  const closePalette = useCallback(() => setPalette(false), []);
  const openPalette = useCallback(() => {
    paletteRestoreRef.current = document.activeElement;
    setDrawer(false);
    setPalette(true);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const update = (event) => setSystemDark(event.matches);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);
  useEffect(() => {
    const keydown = (event) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openPalette(); }
    };
    document.addEventListener("keydown", keydown);
    return () => document.removeEventListener("keydown", keydown);
  }, [openPalette]);
  useEffect(() => { setDrawer(false); }, [location.pathname]);
  useEffect(() => { if (desktop) setDrawer(false); }, [desktop]);

  const selectTheme = () => {
    const next = theme === "dark" ? "light" : "dark";
    setThemeOverride(next);
    const result = writeThemePreference(localStorage, next);
    setThemeWarning(result.ok ? "" : "Theme preference storage is unavailable; your selection may not persist.");
  };
  const ws = WS[stream.status] || WS.closed;
  const provider = health.data?.backend || health.data?.llm_backend;
  const shellBlocked = palette || (!desktop && drawer);

  return (
    <div className="app-frame">
      <a href="#main-content" className="skip-link">Skip to main content</a>
      <Sidebar blocked={palette} modal={!desktop} open={desktop || drawer} onClose={closeDrawer} restoreRef={menuButtonRef} stream={stream} />
      <div className="app-column" inert={shellBlocked ? true : undefined} aria-hidden={shellBlocked ? "true" : undefined}>
        <header className="topbar">
          <button ref={menuButtonRef} className="icon-button min-h-11 lg:hidden" onClick={() => setDrawer(true)} aria-label="Open navigation" aria-expanded={drawer}><Icon name="menu" /></button>
          <div className="route-context"><span>SkyN3t</span><strong>{route.label}</strong></div>
          <button ref={searchButtonRef} className="page-search-button min-h-11" onClick={openPalette} aria-label="Find a page" aria-haspopup="dialog"><Icon name="search" /><span>Find a page</span><kbd>⌘K</kbd></button>
          <div className="topbar-actions">
            <span className={`connection-chip ${ws.cls}`} title={provider ? `Provider: ${provider}` : "Provider unavailable"}><span className="status-dot" />{ws.label}{provider ? ` · ${provider}` : ""}</span>
            <button className="icon-button min-h-11" onClick={selectTheme} aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}><Icon name={theme === "dark" ? "sun" : "moon"} /></button>
            <NavLink to="/studio" className="btn-ember topbar-build"><Icon name="plus" size={18} />New build</NavLink>
          </div>
        </header>
        <main id="main-content" tabIndex={-1} className="main-content">
          <div className="content-wrap">
            {themeWarning ? <div role="alert" className="notice notice-warning mb-5">{themeWarning}</div> : null}
            <StaleCodeBanner />
            <PendingApprovalsBanner stream={stream} />
            <ViewErrorBoundary key={location.pathname}>
              <Suspense fallback={<div role="status" className="loading-state"><span className="status-dot status-warn" />Loading {route.label}…</div>}>
                <Routes>
                  <Route path="/" element={<Navigate to="/overview" replace />} />
                  <Route path="/overview" element={<Overview stream={stream} />} />
                  <Route path="/agents" element={<Agents stream={stream} />} />
                  <Route path="/studio" element={<Studio stream={stream} />} />
                  <Route path="/cortex" element={<Cortex stream={stream} />} />
                  <Route path="/brain" element={<Brain stream={stream} />} />
                  <Route path="/skills" element={<Skills />} />
                  <Route path="/activity" element={<Activity stream={stream} />} />
                  <Route path="/settings" element={<Settings />} />
                  <Route path="/projects" element={<Projects stream={stream} />} />
                  <Route path="/workspace" element={<Workspace stream={stream} />} />
                  <Route path="*" element={<Navigate to="/overview" replace />} />
                </Routes>
              </Suspense>
            </ViewErrorBoundary>
          </div>
        </main>
      </div>
      <CommandPalette
        fallbackRestoreRef={searchButtonRef}
        open={palette}
        onClose={closePalette}
        restoreRef={paletteRestoreRef}
      />
    </div>
  );
}
