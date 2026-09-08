---
slug: won-tauri-shape
title: Winning tauri build shape
stack: tauri
tags: build-distilled, design, desktop, frontend, react, tauri, ui, web
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: build-distilled
---

A real **tauri** build scored 100 (go) with this structure — reuse it as a starting shape:

- Entrypoint(s): index.html, src/App.jsx
- Files (14 shown): index.html, postcss.config.js, scripts/make-icons.js, src-tauri/build.rs, src-tauri/src/main.rs, src/App.jsx, src/assets/Logo.jsx, src/components/ActivityRail.jsx, src/components/CommandPalette.jsx, src/components/DiffView.jsx, src/components/EditorPane.jsx, src/components/FindReplaceOverlay.jsx, src/components/MacroRecorder.jsx, src/components/MarkdownPreview.jsx

Example brief it satisfied: Build GreenText, a fast standalone cross-platform DESKTOP text and code editor (Mac + Windows) — a BBEdit replacement — as a Monaco + React + Vite frontend in a

## Reference code from the winning build
Real, working code from this win — adapt these patterns:

#### `index.html`
```
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <link rel="icon" type="image/svg+xml" href="/greentext-icon.svg" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>GreenText</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
```

#### `src/App.jsx`
```
import { useEffect, useCallback } from 'react';
import { useAppStore } from './store/appStore';
import { useSessionStore } from './store/sessionStore';
import { useTheme } from './hooks/useTheme';
import TitleBar from './components/TitleBar';
import ActivityRail from './components/ActivityRail';
import Sidebar from './components/Sidebar';
import TabBar from './components/TabBar';
import EditorPane from './components/EditorPane';
import StatusBar from './components/StatusBar';
import CommandPalette from './components/CommandPalette';
import QuickOpen from './components/QuickOpen';
import {
  openFolderDialog,
  openFileDialog,
  saveFileDialog,
  readFileUtf8,
  writeFileUtf8,
  listDirectory,
  confirmDiscard,
  showError,
} from './lib/fsAdapter';
import { fileName, detectLanguage } from './lib/languageMap';

export default function App() {
  const store = useAppStore();
  const {
    buffers,
    activeBufferId,
    addBuffer,
    openBuffer,
    closeBuffer,
    markSaved,
    togglePreview,
    setActiveRail,
    openPalette,
    openQuickOpen,
  } = store;

  const { lastOpenFolder, lastOpenFiles, setLastOpenFolder, recordOpenFile, removeOpenFile } = useSessionStore();
  useTheme();

  useEffect(() => {
    store.initBuffers();
  }, []);

  useEffect(() => {
    async function restore() {
      if (lastOpenFolder) {
        try {
          const listed = await listDirectory(lastOpenFolder);
          store.setOpenFolder(lastOpenFolder, listed);
        } catch {
        
/* …truncated… */
```
