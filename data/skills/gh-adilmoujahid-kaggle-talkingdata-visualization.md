---
slug: gh-adilmoujahid-kaggle-talkingdata-visualization
title: adilmoujahid/kaggle-talkingdata-visualization: 2016 Flask/Kaggle-dataset tutorial (held)
stack: react
tags: data-visualization, flask, python, reference, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-adilmoujahid-kaggle-talkingdata-visualization
description: "Review hold: No machine-verified license (GitHub license API: Not Found/404); the procedure also targets end-of-life Python 2.7 and a gated, account-required Kaggle competition dataset, so it cannot be generalized as safe reusable guidance."
compatibility: react
metadata:
  skyn3t-advisory-sha256: sha256:478734b0680a106703a812f9a50cc95a1b66f1ab6c90fcdbfe150fa7b168a302
  skyn3t-content-sha256: sha256:abfa6e27f51b81df754ee55327adc8414e80c6826ca21940803545c04b2425c7
  skyn3t-evidence-index: evidence/reviewed/gh-adilmoujahid-kaggle-talkingdata-visualization.receipt.json
  skyn3t-evidence-path: evidence/reviewed/abfa6e27f51b81df754ee55327adc8414e80c6826ca21940803545c04b2425c7.source
  skyn3t-hold-reason: "No machine-verified license (GitHub license API: Not Found/404); the procedure also targets end-of-life Python 2.7 and a gated, account-required Kaggle competition dataset, so it cannot be generalized as safe reusable guidance."
  skyn3t-pinned-revision: 2ee6da16a85e3f6250bffae5f523189ea4e91d0c
  skyn3t-previous-body-sha256: sha256:919838c84307cc4ba52757d3ceae46098df1f1eca6553cdcaa9528ed217ff076
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/adilmoujahid/kaggle-talkingdata-visualization
---

Review hold: No machine-verified license (GitHub license API: Not Found/404); the procedure also targets end-of-life Python 2.7 and a gated, account-required Kaggle competition dataset, so it cannot be generalized as safe reusable guidance.

This repository accompanies a 2016 blog post visualizing geospatial mobile-usage data with D3.js, DC.js, Leaflet.js and Python. Documented dependencies are Python 2.7, Pandas (recommended via the Anaconda distribution), Flask, and Shapely, installed with `pip install flask shapely`. The documented run procedure is: install dependencies, download three named CSV files from a specific Kaggle competition (which requires creating a Kaggle account and agreeing to that competition's rules), place them in an `input` folder, then run `python app.py` from the repo root.

This record is held rather than activated. The GitHub license API returns Not Found for this repository. Independently, the procedure depends on Python 2.7 (end-of-life since 2020) and on a gated, competition-specific Kaggle dataset that requires an external account and rules acceptance, so it is not a portably reusable pattern for a general factory context, and no verification step for the Flask app is shown in the source.
