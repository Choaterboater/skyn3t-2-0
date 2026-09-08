---
slug: postgres-query-and-schema-engineering
title: PostgreSQL query, schema, and connection engineering
stack: fastapi
tags: database, postgres, postgresql, stack:nextjs, stack:node, stack:rag, stage:code, stage:verify, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: postgres-query-and-schema-engineering
description: "Apply only when the actual project uses PostgreSQL. For SQLite, another database, or an unspecified database, this advice is not applicable; keep the project's existing backend choice."
license: MIT
compatibility: fastapi
metadata:
  skyn3t-advisory-sha256: sha256:6b8ea5f5cf6a5d57bfedb26a6431f0cce15e83f42b9eb9e02e59578c5d5322dd
  skyn3t-content-sha256: sha256:ad65e776f45e761bafc609d3f506c77c1032225aa65e0c4c02f241b8071e5855
  skyn3t-evidence-index: evidence/reviewed/postgres-query-and-schema-engineering.receipt.json
  skyn3t-evidence-path: evidence/reviewed/ad65e776f45e761bafc609d3f506c77c1032225aa65e0c4c02f241b8071e5855.source
  skyn3t-pinned-revision: 8331f910845103c08d51f6ca1d86ebb7d1f745e3
  skyn3t-review-status: approved
  skyn3t-source-path: skills/supabase-postgres-best-practices/SKILL.md
  skyn3t-source-url: https://github.com/supabase/agent-skills
---

Apply only when the actual project uses PostgreSQL. For SQLite, another database, or an unspecified database, this advice is not applicable; keep the project's existing backend choice.

1. Identify the installed PostgreSQL version, access library, pooler, relevant schema, and representative slow query. Capture a baseline plan and latency under a stated dataset and concurrency; completion means the bottleneck is reproducible rather than inferred from a framework name.
2. Read the plan before adding indexes. Relate filters, joins, ordering, row estimates, and scan work to the query's real access pattern. Use a safe test database for EXPLAIN ANALYZE: it executes the statement, so it is not a read-only inspection command for arbitrary SQL.
3. Choose the smallest suitable index and re-measure reads, writes, storage, and migration impact. Avoid duplicate or unused indexes, and verify behavior at representative data sizes. A faster tiny fixture alone is not production evidence.
4. Budget connections across application replicas and background workers. Bound pool size and acquisition time; release connections on errors and cancellation. Check pooler-mode restrictions against the application's transaction/session behavior instead of copying a universal pool setting.
5. Review schema types, constraints, lock duration, and migration reversibility together. Exercise the migration and application path against a disposable database before applying a production change; preserve authorization and row-level isolation where the application requires them.

Done when measured evidence supports the change, query results and access boundaries are unchanged, connection exhaustion has a visible failure path, and the migration/recovery procedure is documented. This is PostgreSQL advice, not a requirement to use Supabase or a substitute for RAG relevance evaluation.
