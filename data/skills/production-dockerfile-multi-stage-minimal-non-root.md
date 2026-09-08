---
slug: production-dockerfile-multi-stage-minimal-non-root
title: Production Dockerfile: multi-stage, minimal, non-root
stack: generic
tags: delivery, docker, dockerfile, docs, documentation, generic, github-curated, image-size, node, packaging
uses: 26
helpful: 9
quality_sum: 15.0870
score: 0.580
source: github-curated
name: production-dockerfile-multi-stage-minimal-non-root
description: "Use a multi-stage build: compile/install dependencies in a builder stage and copy only runtime artifacts into a small final image (slim/alpine/distroless), so build tools never ship to production. Pin the base image to a specific version (ideally a digest) for reproducibility, and order instructions so dependency installs cache before copying frequently-changing source. Add a .dockerignore to keep .git, node_modules, secrets, and tests out of the build context. Create and switch to a non-root USER before the entrypoint, expose the port, and use exec-form CMD so signals propagate for clean shutdown."
compatibility: generic
metadata:
  skyn3t-advisory-sha256: sha256:867f949051eabe1ef8d0fdb0fc5c3641161f5aacdc3b7abddc74382e35067559
  skyn3t-content-sha256: sha256:b4291fa371c5949dec65ede5bb8b1473c44393f583f82c7f3776756cfbac7028
  skyn3t-evidence-index: evidence/reviewed/production-dockerfile-multi-stage-minimal-non-root.receipt.json
  skyn3t-evidence-path: evidence/reviewed/b4291fa371c5949dec65ede5bb8b1473c44393f583f82c7f3776756cfbac7028.source
  skyn3t-previous-body-sha256: sha256:867f949051eabe1ef8d0fdb0fc5c3641161f5aacdc3b7abddc74382e35067559
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://docs.docker.com/build/building/best-practices/
---

Use a multi-stage build: compile/install dependencies in a builder stage and copy only runtime artifacts into a small final image (slim/alpine/distroless), so build tools never ship to production. Pin the base image to a specific version (ideally a digest) for reproducibility, and order instructions so dependency installs cache before copying frequently-changing source. Add a .dockerignore to keep .git, node_modules, secrets, and tests out of the build context. Create and switch to a non-root USER before the entrypoint, expose the port, and use exec-form CMD so signals propagate for clean shutdown.

Source: https://docs.docker.com/build/building/best-practices/
