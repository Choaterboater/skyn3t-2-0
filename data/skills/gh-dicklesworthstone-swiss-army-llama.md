---
slug: gh-dicklesworthstone-swiss-army-llama
title: Dicklesworthstone/swiss_army_llama: FastAPI LLM Embedding/Document Microservice (license unverified)
stack: python
tags: api, backend, python, github-distilled, hygiene:quarantine, review-held
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-dicklesworthstone-swiss-army-llama
description: "Review hold: No LICENSE file is present in the pinned repository snapshot (license path absent) even though the README text asserts an MIT license; the artifact itself could not be confirmed, so activation is withheld."
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:47defdfeac7cec1f3cd436b2a742e33ffb68c091ff573ef0eed5a8f5e8b14781
  skyn3t-content-sha256: sha256:15a27a7f40ae30593ca3ebccabc0f2d9472d24ac65be8dbf9cd7d6a1bd29a9b6
  skyn3t-evidence-index: evidence/reviewed/gh-dicklesworthstone-swiss-army-llama.receipt.json
  skyn3t-evidence-path: evidence/reviewed/15a27a7f40ae30593ca3ebccabc0f2d9472d24ac65be8dbf9cd7d6a1bd29a9b6.source
  skyn3t-hold-reason: "No LICENSE file is present in the pinned repository snapshot (license path absent) even though the README text asserts an MIT license; the artifact itself could not be confirmed, so activation is withheld."
  skyn3t-pinned-revision: 7bd155410ff2cdf71b4ddf4ccd5a626a600690b3
  skyn3t-previous-body-sha256: sha256:a30883032853b22a8a056b0e8a5a242c38969385bad8213aca775b291575f167
  skyn3t-review-status: held
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/dicklesworthstone/swiss_army_llama
---

Review hold: No LICENSE file is present in the pinned repository snapshot (license path absent) even though the README text asserts an MIT license; the artifact itself could not be confirmed, so activation is withheld.

Applicability: a FastAPI-based microservice pattern exposing local LLM embeddings, document-format extraction (via textract, supporting PDF/DOC/audio and more), and a RAM-disk-backed model cache -- aimed at self-hosted semantic search/embedding backends.

Why held (activation gate, not content quality): the pinned source snapshot shows no LICENSE file (path absent) even though the README body itself states "This project is licensed under the MIT License." Because the actual LICENSE artifact could not be confirmed in the repository at the pinned revision, redistribution terms are unverifiable, so this must be held rather than activated. Last commit 2025-02-27.

Documented pattern (for reference, not verified safe to run as-is):
1. System deps: sudo apt-get install libxml2-dev libxslt1-dev antiword unrtf poppler-utils pstotext tesseract-ocr flac ffmpeg lame libmad0 libsox-fmt-mp3 sox libjpeg-dev swig -y.
2. Python deps from the listed package set (fastapi, llama-cpp-python, faiss-cpu, redis, sqlalchemy, and others named in the README).
3. Run: python swiss_army_llama.py; the server binds 0.0.0.0 on the port from SWISS_ARMY_LLAMA_SERVER_LISTEN_PORT.
4. Verify via the Swagger UI at http://localhost:<port>.
5. Configuration is via a .env file (concurrency limits, model name, context size, RAM disk toggle/path/size).

Caution: the optional RAM-disk setup requires editing the sudoers file to grant password-less sudo mount/umount for a specific tmpfs path -- a real privilege surface. If ever activated, this must be scoped tightly to the exact mount/umount commands shown, never a broader NOPASSWD grant.
