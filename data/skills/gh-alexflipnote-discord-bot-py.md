---
slug: gh-alexflipnote-discord-bot-py
title: discord.py bot bootstrap pattern (token via .env, process-supervision options)
stack: python
tags: bot, cli, discord, python, reference, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-alexflipnote-discord-bot-py
description: "For an explicitly selected discord.py bot, keep command handling, domain logic, and configuration separate. Inspect the installed discord.py/Python requirements and existing project entrypoint before adapting this template; a historical rewrite label does not establish compatibility with every current API."
license: MIT
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:c0195b9ab47152a271c0b72383e912e0e25a2d7c7fce06899128513829ce354b
  skyn3t-content-sha256: sha256:0ed1ad839b444176154085f26bb08792d3244b56cfd50b8b35fcc53c76e497aa
  skyn3t-evidence-index: evidence/reviewed/gh-alexflipnote-discord-bot-py.receipt.json
  skyn3t-evidence-path: evidence/reviewed/0ed1ad839b444176154085f26bb08792d3244b56cfd50b8b35fcc53c76e497aa.source
  skyn3t-pinned-revision: c8ea8e4719d42b4a083e22fa4447caed75b04e06
  skyn3t-previous-body-sha256: sha256:2f2f11538397af549bef465deff0cc8d58547e0f99848674f31688c1cb91c20e
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/alexflipnote/discord_bot.py
---

For an explicitly selected discord.py bot, keep command handling, domain logic, and configuration separate. Inspect the installed discord.py/Python requirements and existing project entrypoint before adapting this template; a historical rewrite label does not establish compatibility with every current API.

Store the bot token in private runtime configuration and retain a safe .env.example. Use only an authorized bot application and test server. Message-prefix commands may require the privileged Message Content intent in both application settings and client configuration; enable it only when that feature is required and approved. Interaction/slash-command designs need their own appropriate scopes rather than inheriting unnecessary message access.

Run the existing bot command in the project environment. Choose a process supervisor only as a separate deployment decision; this pattern does not authorize global PM2 installation, detached containers, or changes to unrelated running services.

Verify a harmless command in the authorized test server, permission denial, missing configuration, reconnect behavior, and clean shutdown. Presence as online is not proof that the intended command works. Redact tokens and message content from diagnostic logs, and report missing intents or permissions explicitly.
