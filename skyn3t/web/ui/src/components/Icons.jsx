import React from "react";

const paths = {
  home: <><path d="m3 10 9-7 9 7"/><path d="M5 9v11h14V9M9 20v-6h6v6"/></>,
  projects: <><path d="M3 6h7l2 2h9v11H3z"/><path d="M3 6V4h7l2 2"/></>,
  build: <><path d="M12 3v6M9 6h6"/><path d="M5 11h14v9H5z"/><path d="M8 15h8"/></>,
  workspace: <><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M9 9h12"/></>,
  agents: <><circle cx="12" cy="7" r="3"/><path d="M6 21v-2a6 6 0 0 1 12 0v2M4 9a3 3 0 0 0 0 6M20 9a3 3 0 0 1 0 6"/></>,
  cortex: <><path d="M8 4a4 4 0 0 0-3 6 4 4 0 0 0 1 7 4 4 0 0 0 6 3V4a4 4 0 0 0-4 0Z"/><path d="M16 4a4 4 0 0 1 3 6 4 4 0 0 1-1 7 4 4 0 0 1-6 3M8 9h4M12 14h4"/></>,
  skills: <><path d="M12 2 9 8l-6 1 4.5 4.5L6.5 20l5.5-3 5.5 3-1-6.5L21 9l-6-1z"/></>,
  brain: <><path d="M9 4a3 3 0 0 0-4 4 3 3 0 0 0 0 5 3 3 0 0 0 4 4M15 4a3 3 0 0 1 4 4 3 3 0 0 1 0 5 3 3 0 0 1-4 4M9 4v16M15 4v16M9 9h6M9 15h6"/></>,
  activity: <><path d="M4 18V9M10 18V4M16 18v-6M22 18H2"/></>,
  settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a2 2 0 0 0 .4 2.2l.1.1-2.6 2.6-.1-.1A2 2 0 0 0 15 19.4a2 2 0 0 0-1.6 1.4V21H9.6v-.2A2 2 0 0 0 8 19.4a2 2 0 0 0-2.2.4l-.1.1-2.6-2.6.1-.1A2 2 0 0 0 3.6 15a2 2 0 0 0-1.4-1.6H2V9.6h.2A2 2 0 0 0 3.6 8a2 2 0 0 0-.4-2.2l-.1-.1 2.6-2.6.1.1A2 2 0 0 0 8 3.6a2 2 0 0 0 1.6-1.4V2h3.8v.2A2 2 0 0 0 15 3.6a2 2 0 0 0 2.2-.4l.1-.1 2.6 2.6-.1.1A2 2 0 0 0 19.4 8a2 2 0 0 0 1.4 1.6h.2v3.8h-.2a2 2 0 0 0-1.4 1.6Z"/></>,
  search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
  menu: <><path d="M4 7h16M4 12h16M4 17h16"/></>,
  close: <><path d="m6 6 12 12M18 6 6 18"/></>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></>,
  moon: <path d="M20 15.5A8 8 0 0 1 8.5 4 8 8 0 1 0 20 15.5Z"/>,
  plus: <path d="M12 5v14M5 12h14"/>,
};

export default function Icon({ name, size = 20, className = "" }) {
  return <svg aria-hidden="true" className={className} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">{paths[name] || paths.home}</svg>;
}
