---
slug: gh-georgegriff-react-dnd-kit-tailwind-shadcn-ui
title: georgegriff/react-dnd-kit-tailwind-shadcn-ui: Accessible Kanban Example (setup steps not in source)
stack: react
tags: design, frontend, react, typescript, ui, web, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-georgegriff-react-dnd-kit-tailwind-shadcn-ui
description: "Review hold: The pinned README names the tech stack and links to demo/developer.md, but the actual setup/run instructions live in developer.md, which is not included in this source snapshot; no install command or file structure is present in the fetched text."
license: MIT
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:d26d14a74211dfec82403f49660741aec0ddd7985b8562d522bf517a4a00223e
  skyn3t-content-sha256: sha256:bbc8d19430f6078d23c0ccb751fe8e0837657aeac91a6cc9cc834332fa3b74d6
  skyn3t-evidence-index: evidence/reviewed/gh-georgegriff-react-dnd-kit-tailwind-shadcn-ui.receipt.json
  skyn3t-evidence-path: evidence/reviewed/bbc8d19430f6078d23c0ccb751fe8e0837657aeac91a6cc9cc834332fa3b74d6.source
  skyn3t-hold-reason: "The pinned README names the tech stack and links to demo/developer.md, but the actual setup/run instructions live in developer.md, which is not included in this source snapshot; no install command or file structure is present in the fetched text."
  skyn3t-pinned-revision: 3e1dbb0b872125191d5f9c3686060f64d304b99e
  skyn3t-previous-body-sha256: sha256:5c0f65b93ae6260cedea9aea52f9e87fc946c18799e282a2493b097d573a8701
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/georgegriff/react-dnd-kit-tailwind-shadcn-ui
---

Review hold: The pinned README names the tech stack and links to demo/developer.md, but the actual setup/run instructions live in developer.md, which is not included in this source snapshot; no install command or file structure is present in the fetched text.

Applicability: demonstrates building an accessible drag-and-drop Kanban board with React, @dnd-kit, Tailwind CSS, and shadcn/ui. MIT licensed, last commit 2024-05-18, with a live demo at the project's GitHub Pages URL.

Why held: the pinned README names the tech stack and links to a demo and to a separate developer.md file for actual setup/run instructions, but that developer.md content is not included in this source snapshot. There is no install command, script name, or file structure in the fetched text itself to turn into a verified procedure without guessing.

What is confirmed from this snapshot: the stack is React + @dnd-kit (accessible drag-and-drop primitives) + Tailwind CSS + shadcn/ui, and a working live demo exists, meaning the pattern itself (accessible DnD Kanban composed from headless dnd-kit primitives plus shadcn/ui styled components) is a real, demonstrable one worth revisiting.

Recommendation: hold as reference-only pending a fetch of the linked developer.md, which is where the actual install/run commands live. Do not fabricate npm scripts or a folder layout for this entry; only activate once that file's content can be read and verified.
