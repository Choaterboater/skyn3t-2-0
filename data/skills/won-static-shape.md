---
slug: won-static-shape
title: Winning static build shape
stack: static
tags: build-distilled, design, frontend, static, testing, ui, verification, web
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: build-distilled
---

A real **static** build scored 74 (go) with this structure — reuse it as a starting shape:

- Entrypoint(s): index.html, js/main.js
- Files (9 shown): css/components.css, css/responsive.css, css/styles.css, index.html, js/main.js, js/menu.js, src/config.js, tests/main.test.js, tests/menu.test.js

Example brief it satisfied: a simple static landing page for a neighborhood bakery with a menu section and contact info

## Reference code from the winning build
Real, working code from this win — adapt these patterns:

#### `index.html`
```
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />

  <title>Golden Crust Bakery | Fresh Bread, Pastries & Coffee on Elm Street</title>
  <meta name="description" content="Golden Crust is a neighborhood bakery on Elm Street in Riverton, baking fresh sourdough, croissants, cakes, and savory bites every morning since 1998. See our daily menu, hours, and contact info." />
  <meta name="keywords" content="bakery, sourdough, croissants, coffee, Riverton, Elm Street, fresh bread, pastries, neighborhood bakery" />
  <meta name="author" content="Golden Crust Bakery" />
  <meta name="theme-color" content="#c98a3d" />

  <!-- Open Graph -->
  <meta property="og:type" content="website" />
  <meta property="og:title" content="Golden Crust Bakery | Fresh Bread & Pastries on Elm Street" />
  <meta property="og:description" content="Baked fresh every morning since 1998. Sourdough, croissants, cakes, and single-origin coffee in the heart of Riverton." />
  <meta property="og:url" content="https://goldencrust.example" />
  <meta property="og:locale" content="en_US" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="Golden Crust Bakery | Fresh Bread & Pastries" />
  <meta name="twitter:description" content="Baked fresh every morning since 1998 on Elm Street, Riverton." />

  <!-- Fonts -->
  <link rel="preconnect" href="https://fonts.
/* …truncated… */
```

#### `js/main.js`
```
/* =====================================================================
   Golden Crust Bakery — Main UI Logic
   Handles DOMContentLoaded bootstrap, mobile nav toggle, smooth scroll,
   active-link tracking, hours "today" highlight, contact form validation,
   footer year stamping, and structured-data injection.
   ===================================================================== */

(function () {
  "use strict";

  /** Return a defensive array copy. */
  function toArray(nodeList) {
    return Array.prototype.slice.call(nodeList);
  }

  /** Debounce helper. */
  function debounce(fn, wait) {
    var t;
    return function () {
      var ctx = this;
      var args = arguments;
      clearTimeout(t);
      t = setTimeout(function () {
        fn.apply(ctx, args);
      }, wait);
    };
  }

  /** Determine the day index where Monday = 0 (matches JSON-LD closed-on-Monday). */
  function getBusinessDayIndex(date) {
    var jsDay = date.getDay(); // 0 = Sunday ... 6 = Saturday
    // Map to Monday-first order: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    return (jsDay + 6) % 7;
  }

  /** Mobile navigation toggle. */
  function initNavToggle() {
    var toggle = document.querySelector(".nav-toggle");
    var nav = document.getElementById("primary-nav");
    if (!toggle || !nav) return;

    function closeNav() {
      nav.classList.remove("is-open");
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("aria-label", "Open menu");
    }

  
/* …truncated… */
```
