---
slug: dockerize-fastapi-with-layer-cached-deps-and-one-process-per-container
title: Dockerize FastAPI with layer-cached deps and one process per container
stack: fastapi
tags: api, backend, containers, delivery, deployment, docker, fastapi, github-curated, packaging, production, python
uses: 19
helpful: 15
quality_sum: 12.6680
score: 0.667
source: github-curated
name: dockerize-fastapi-with-layer-cached-deps-and-one-process-per-container
description: "Start from an official python:3.x image (not the deprecated tiangolo/uvicorn-gunicorn base), and copy requirements.txt and pip install before copying app code so the dependency layer stays cached across code changes. Run the server with exec-form CMD [\"fastapi\", \"run\", \"app/main.py\", \"--port\", \"80\"] so SIGTERM reaches the process for graceful shutdown, adding --proxy-headers when behind a TLS-terminating reverse proxy. Run a single process per container and scale with replicas under an orchestrator; use --workers N only for simple single-server Compose deploys."
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:f7b95b19e955a45fd28e423c5a08c81a9f2060288d49b4b55e916af73866297d
  skyn3t-content-sha256: sha256:c9523abf21763222968091c07a74b95d69c087c6db701c8fd82213ca0cb8cb2e
  skyn3t-evidence-index: evidence/reviewed/dockerize-fastapi-with-layer-cached-deps-and-one-process-per-container.receipt.json
  skyn3t-evidence-path: evidence/reviewed/c9523abf21763222968091c07a74b95d69c087c6db701c8fd82213ca0cb8cb2e.source
  skyn3t-previous-body-sha256: sha256:f7b95b19e955a45fd28e423c5a08c81a9f2060288d49b4b55e916af73866297d
  skyn3t-review-status: approved
  skyn3t-source-path: web-document
  skyn3t-source-url: https://fastapi.tiangolo.com/deployment/docker/
---

Start from an official python:3.x image (not the deprecated tiangolo/uvicorn-gunicorn base), and copy requirements.txt and pip install before copying app code so the dependency layer stays cached across code changes. Run the server with exec-form CMD ["fastapi", "run", "app/main.py", "--port", "80"] so SIGTERM reaches the process for graceful shutdown, adding --proxy-headers when behind a TLS-terminating reverse proxy. Run a single process per container and scale with replicas under an orchestrator; use --workers N only for simple single-server Compose deploys.

Source: https://fastapi.tiangolo.com/deployment/docker/
