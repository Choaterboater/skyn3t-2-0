---
slug: rag-retrieval-and-grounding-evaluation
title: RAG retrieval quality and answer-grounding evaluation
stack: rag
tags: evaluation, rag, retrieval, stage:qa_playtest, stage:verify, github-distilled, external-promoted
uses: 0
helpful: 0
quality_sum: 0.0000
score: 0.500
source: github-distilled
name: rag-retrieval-and-grounding-evaluation
description: "Apply to retrieval-bearing applications. Keep the existing marker-document smoke test, then evaluate retrieval and generated answers separately. This is framework-independent guidance from an explicitly licensed historical source, not a maintained evaluation framework or an installed verifier."
license: MIT
compatibility: "Framework-independent evaluation advice; requires a fixed corpus and independently reviewed relevance labels."
metadata:
  skyn3t-advisory-sha256: sha256:8fd5221041ae9d1cf4e8e33d64abbed8cf8666f3a8695cbd8989ceca2b41484c
  skyn3t-content-sha256: sha256:e22a7bff31a73a9a82722c771326acf13121622a8caca6221fcbea111c9bf88d
  skyn3t-evidence-index: evidence/reviewed/rag-retrieval-and-grounding-evaluation.receipt.json
  skyn3t-evidence-path: evidence/reviewed/e22a7bff31a73a9a82722c771326acf13121622a8caca6221fcbea111c9bf88d.source
  skyn3t-pinned-revision: 22418da2bfb159f28a1b0dcf64e969e14ae56c99
  skyn3t-review-status: approved
  skyn3t-source-path: skills/evaluate-rag/SKILL.md
  skyn3t-source-review-exception: "Only framework-independent metric and evaluator-calibration concepts were adapted from the explicitly MIT-licensed historical revision. No archived runtime implementation or unlicensed successor content is imported."
  skyn3t-source-url: https://github.com/hamelsmu/evals-skills
---

Apply to retrieval-bearing applications. Keep the existing marker-document smoke test, then evaluate retrieval and generated answers separately. This is framework-independent guidance from an explicitly licensed historical source, not a maintained evaluation framework or an installed verifier.

1. Freeze a small representative query set, corpus/chunk-ID snapshot, relevant chunks, and supporting answer spans. Include distractors, multi-hop questions, and questions the corpus cannot answer. Use independently reviewed labels; model-generated answers are not their own ground truth.
2. Record the chosen k and score retrieved IDs before generated prose. Measure coverage with recall and ranking quality with an appropriate ranking metric; document denominators, duplicate handling, and empty-result conventions. For multi-hop questions, check that every required evidence piece was retrieved. Treat unanswerable queries as abstention cases rather than dividing by an empty relevant set.
3. Check that emitted citation IDs resolve to retained evidence. Separately determine whether the cited text supports each factual claim and whether the answer addresses the question. A valid ID or similar-sounding answer does not establish grounding; this skill does not provide an automatic entailment verifier.
4. Change chunking, retrieval, or reranking settings one at a time and compare on unchanged examples. Keep retrieval misses, ranking failures, unsupported generation, and latency/cost observations separate so a generation improvement cannot hide a retrieval regression.
5. If an already-approved LLM judge is used, calibrate it on independent human passing and failing labels. Keep prompt examples, tuning cases, and held-out cases separate; report false passes and false failures with class counts and uncertainty. Record the judge model/prompt/configuration and recalibrate after changes. Without those labels, report grading as uncalibrated rather than enabling a judge-based acceptance gate.

Done when the evaluation can be rerun against the retained corpus and examples, failures have an attributable category, and claimed improvements survive the same held-out questions. A probabilistic judge must not overrule a failed deterministic citation-ID, schema, or delivery check. No hosted model, synthetic-data generator, or additional package is required by this advisory.
