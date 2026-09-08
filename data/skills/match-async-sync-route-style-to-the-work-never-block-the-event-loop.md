---
slug: match-async-sync-route-style-to-the-work-never-block-the-event-loop
title: Match async/sync route style to the work, never block the event loop
stack: fastapi
tags: api, async, backend, concurrency, event-loop, fastapi, github-curated, python
uses: 1
helpful: 1
quality_sum: 1.0000
score: 1.000
source: github-curated
name: match-async-sync-route-style-to-the-work-never-block-the-event-loop
description: "Use async def only when the handler awaits genuinely non-blocking I/O (async DB drivers, httpx, asyncio). If you must call a blocking SDK, wrap it with Starlette's run_in_threadpool or just declare the route with plain def so FastAPI runs it in its threadpool; calling blocking code inside an async route freezes the loop and stalls every request. Offload CPU-heavy work to a task queue (Celery/Arq/RQ), not threads, since the GIL blocks real CPU parallelism. Reserve BackgroundTasks for short, non-critical fire-and-forget work only, as it has no retries or persistence."
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:dc26b1074d950c5fe1ec6123e678f1de7ac08866f3a98f03299173f18e01e13b
  skyn3t-content-sha256: sha256:cbe588bc3e79d10181ed661957b7c8f459bc515e2c0bbafd36d37827fe77ac20
  skyn3t-evidence-index: evidence/reviewed/match-async-sync-route-style-to-the-work-never-block-the-event-loop.receipt.json
  skyn3t-evidence-path: evidence/reviewed/cbe588bc3e79d10181ed661957b7c8f459bc515e2c0bbafd36d37827fe77ac20.source
  skyn3t-pinned-revision: 5e00aa6095521f0d00e4eec2ef0afa44cd566af4
  skyn3t-previous-body-sha256: sha256:dc26b1074d950c5fe1ec6123e678f1de7ac08866f3a98f03299173f18e01e13b
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/zhanymkanov/fastapi-best-practices
---

Use async def only when the handler awaits genuinely non-blocking I/O (async DB drivers, httpx, asyncio). If you must call a blocking SDK, wrap it with Starlette's run_in_threadpool or just declare the route with plain def so FastAPI runs it in its threadpool; calling blocking code inside an async route freezes the loop and stalls every request. Offload CPU-heavy work to a task queue (Celery/Arq/RQ), not threads, since the GIL blocks real CPU parallelism. Reserve BackgroundTasks for short, non-critical fire-and-forget work only, as it has no retries or persistence.

Source: https://github.com/zhanymkanov/fastapi-best-practices
