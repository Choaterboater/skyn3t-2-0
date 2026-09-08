---
slug: gh-freedmand-semantra-python
title: freedmand/semantra-python: Local Semantic Document Search CLI + Web UI
stack: python
tags: cli, python, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: gh-freedmand-semantra-python
description: "For an explicitly selected local document-search workflow, separate document parsing, embedding/indexing, query ranking, and navigation back to source passages. The inspected Semantra implementation demonstrates cached local embeddings over text/PDF documents and a browser-based search interface."
license: MIT
compatibility: python
metadata:
  skyn3t-advisory-sha256: sha256:fc85cfeb350b115cfdbf6d9d71af58017bcd4c1cc4b187f9cae4f3d50382bc07
  skyn3t-content-sha256: sha256:d51aa750f4f34265374dc7270bcd66965b5837320ed03270f37e384a66341ba4
  skyn3t-evidence-index: evidence/reviewed/gh-freedmand-semantra-python.receipt.json
  skyn3t-evidence-path: evidence/reviewed/d51aa750f4f34265374dc7270bcd66965b5837320ed03270f37e384a66341ba4.source
  skyn3t-pinned-revision: 1aed8fd0057f6b3eb7946e0f351f9c668842774d
  skyn3t-previous-body-sha256: sha256:f530e7fe4fd39c4efb72fd374b0d08744961b9bff2d8ec25c0f4baaf20ace22b
  skyn3t-review-status: approved
  skyn3t-source-path: README.md
  skyn3t-source-url: https://github.com/freedmand/semantra-python
---

For an explicitly selected local document-search workflow, separate document parsing, embedding/indexing, query ranking, and navigation back to source passages. The inspected Semantra implementation demonstrates cached local embeddings over text/PDF documents and a browser-based search interface.

Scope ingestion to the operator-approved files. Select the embedding backend explicitly before processing; local model downloads have disk/network/resource costs, while hosted embeddings send document content to another provider. A failed local setup must not silently switch to OpenAI or another hosted service.

Use the selected project environment or an already approved isolated tool installation. Check the actual package/model requirements rather than installing pipx globally or rewriting PATH as a side effect of importing this guidance. Cache with document and model identity so changed inputs are reindexed deliberately.

Return source-linked results and evaluate relevance on representative queries. Similarity scores depend on the model/index and are not calibrated probabilities; do not adopt a universal 0.50 threshold or claim that every score lies in a specific range without checking the implementation.

Verify a known passage is retrieved and opens at the right source location, a changed document refreshes correctly, an unrelated query does not imply a confident match, and ingestion stays inside the authorized file scope. Report resource/dependency failures visibly and keep the approved privacy boundary intact.
