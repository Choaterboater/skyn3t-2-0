---
slug: accessible-forms-with-react-hook-form-zod-and-semantic-html
title: Accessible forms with React Hook Form, Zod, and semantic HTML
stack: react
tags: a11y, accessibility, design, forms, frontend, github-curated, react, react-hook-form, ui, wcag, web, zod
uses: 5
helpful: 1
quality_sum: 2.7200
score: 0.544
source: github-curated
name: accessible-forms-with-react-hook-form-zod-and-semantic-html
description: "Build forms with React Hook Form driven by a Zod schema via `zodResolver` (`resolver: zodResolver(formSchema)`), so one schema is the source of truth for both validation and the inferred TypeScript type (`type FormFields = z.infer<typeof formSchema>`) \u2014 this exact wiring is demonstrated in a real React Hook Form + Zod walkthrough. Validate on blur (`mode: 'onBlur'`) for a calmer experience than validating on every keystroke."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:4ce224adc41513bfcd46fc2962b1cf720a4981c4304df30a9b044fe94f5c89fe
  skyn3t-content-sha256: sha256:e7b9b5466301ea4df740e2cd37d37ecbe8db56a867ce7e663285632ae59bc492
  skyn3t-evidence-index: evidence/reviewed/accessible-forms-with-react-hook-form-zod-and-semantic-html.receipt.json
  skyn3t-evidence-path: evidence/reviewed/e7b9b5466301ea4df740e2cd37d37ecbe8db56a867ce7e663285632ae59bc492.source
  skyn3t-previous-body-sha256: sha256:8c3db881edae745701a7ff3bde7c2322ecd6a6b23ee5137f738c64ef67e41626
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://timjames.dev/blog/building-forms-with-zod-and-react-hook-form-2geg
---

Build forms with React Hook Form driven by a Zod schema via `zodResolver` (`resolver: zodResolver(formSchema)`), so one schema is the source of truth for both validation and the inferred TypeScript type (`type FormFields = z.infer<typeof formSchema>`) — this exact wiring is demonstrated in a real React Hook Form + Zod walkthrough. Validate on blur (`mode: 'onBlur'`) for a calmer experience than validating on every keystroke.

Use native semantic elements (`input`, `select`, `button`, `fieldset`) with a real `<label htmlFor="id">` tied to every control's `id`, rather than div soup. If you use a component library instead (MUI, Chakra, etc.), confirm it actually renders a real `<label>`/`<input>` pair under the hood — don't assume it does just because it looks like a form.

Per the W3C WAI accessible-forms tutorial, associate each errored control with its message via `aria-describedby`: `<input id="firstname" aria-describedby="firstname_error">`, with the message rendered in an element carrying that id, so screen readers announce it; also set `aria-invalid` on errored controls (WAI technique ARIA21, "Using aria-invalid to Indicate An Error Field"). Per WCAG Success Criterion 1.4.1 (Use of Color), never convey validation state by color alone: "color is not used as the only visual means of conveying information" — pair the red border/text with an icon or explicit message.

Verify: tab through the form with a screen reader (or the accessibility tree in devtools) and confirm each error is announced when its field receives focus, not only visible on screen.
