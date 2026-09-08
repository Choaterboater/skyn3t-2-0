---
slug: gh-chocomeow-vocard
title: ChocoMeow/Vocard: Discord music bot, setup deferred entirely to an external docs site (held)
stack: python
tags: bot, discord, python, reference, github-distilled, hygiene:quarantine, review-held
uses: 1
helpful: 1
quality_sum: 0.7400
score: 0.740
source: github-distilled
name: gh-chocomeow-vocard
description: "Review hold: The actual setup procedure is deferred entirely to an external documentation site (docs.vocard.xyz) not included in the provided source text; this task must not fetch that external site, and reconstructing setup steps from the feature list alone would mean inventing undocumented commands."
license: MIT
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:72854bbf5dba7e458efe1963fadf239bb552a325952d6bf6d95abefc20f56318
  skyn3t-content-sha256: sha256:2bd41829eb031a0c7f39cfc6acba1001071cd947321812a6cb4c504025f36f3d
  skyn3t-evidence-index: evidence/reviewed/gh-chocomeow-vocard.receipt.json
  skyn3t-evidence-path: evidence/reviewed/2bd41829eb031a0c7f39cfc6acba1001071cd947321812a6cb4c504025f36f3d.source
  skyn3t-hold-reason: "The actual setup procedure is deferred entirely to an external documentation site (docs.vocard.xyz) not included in the provided source text; this task must not fetch that external site, and reconstructing setup steps from the feature list alone would mean inventing undocumented commands."
  skyn3t-pinned-revision: 9447b0a41075f53959ad0022d64808abc78732b3
  skyn3t-previous-body-sha256: sha256:27ec2950c20f9e0db1009fc2cafefe5ed6dd80128166f48a7ee3aa80a4c5ebc7
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/chocomeow/vocard
---

Review hold: The actual setup procedure is deferred entirely to an external documentation site (docs.vocard.xyz) not included in the provided source text; this task must not fetch that external site, and reconstructing setup steps from the feature list alone would mean inventing undocumented commands.

Vocard is a Discord music bot supporting YouTube/SoundCloud/Spotify/Twitch/Apple Music playback with slash and message commands, playlists, lyrics, and a one-click installer/premium dashboard offered as separate companion projects. Documented requirements are Python 3.11+ and a Lavalink server (4.0.0+).

This record is held rather than activated. The README explicitly states "Please see the Setup Page in the docs to run this bot yourself" and links to an external documentation site (docs.vocard.xyz); no install, configuration, or run commands are present in the fetched README text itself. Reconstructing a setup procedure from only the feature list and requirements would mean inventing undocumented commands (bot token configuration, Lavalink connection settings, config file format), which this task must not do, and fetching that external docs site is outside this task's scope.
