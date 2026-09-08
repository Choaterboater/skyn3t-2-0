# Additional skills for remaining proof gaps - 2026-09-08

## Recommendation

Curate **`evaluate-rag` and `swift-testing-expert` first**. A third, **`validate-evaluator`**, is useful only when a project actually uses an LLM judge and has independent human labels. These are new to the inspected inventory and do not repeat the [original nine candidates](2026-09-08-skill-discovery.md).

This bounded follow-up found **no suitable, licensed first-party Tauri/Electron app-lifecycle skill or API-contract skill worth adding immediately**. Swift Testing improves native Swift behavior proof, not full desktop launch/window/quit automation. Do not present these three as closing every gap.

Research was read-only, apart from this report. The [skills.sh leaderboard](https://skills.sh/) was checked first as discovery only, then source-owned repositories, actual skill/reference bodies, licenses and commits were read. Observations are dated **2026-09-08 UTC**, with a clock marker at **18:18:12Z**. Adoption counts are observed directory counters, not evidence of trust or effectiveness.

| Priority | Exact skill | Concrete new capability | Curation decision |
| --- | --- | --- | --- |
| P1 | `hamelsmu/evals-skills` / `evaluate-rag` | Gold-chunk retrieval metrics separated from answer grounding | Curate a small advisory from the explicitly MIT-licensed historical pin below |
| P1, Swift only | `AvdLee/Swift-Testing-Agent-Skill` / `swift-testing-expert` | Async completion, actor/resource isolation and correct Swift Testing/XCTest boundaries | Curate native Swift unit/integration proof guidance; preserve existing UI proof |
| P2, conditional | `hamelsmu/evals-skills` / `validate-evaluator` | Establish whether an LLM grader recognizes both real passes and real failures | Curate calibration advice; do not activate judge-based gating without independent labels |

### Important source relocation and license boundary

The two evaluation skills were originally published by Hamel Husain under MIT. That repository is now archived and explicitly points to **`ai-evals-course/evals-skills`**, maintained by Shreya Shankar and Hamel Husain ([migration notice](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/README.md#L3-L12)). The successor is active at `11d35781d43c281baddd3c6a766b12d81c274c29`, but its inspected tree/root and skill frontmatter did not establish a redistribution license ([tree](https://github.com/ai-evals-course/evals-skills/tree/11d35781d43c281baddd3c6a766b12d81c274c29), [current RAG skill](https://github.com/ai-evals-course/evals-skills/blob/11d35781d43c281baddd3c6a766b12d81c274c29/skills/evaluate-rag/SKILL.md#L1-L18), [repository metadata](https://api.github.com/repos/ai-evals-course/evals-skills)).

**Use only the explicitly licensed historical revision for these proposed derivatives.** This is a deliberate, disclosed provenance choice, not a claim that the archived source is the maintained/latest edition. Do not attach the old MIT notice to unreviewed changes from the successor. The material is author-owned evaluation guidance, not a platform vendor's guarantee.

## 1. RAG retrieval and grounding evaluation

**Exact source:** [`hamelsmu/evals-skills:skills/evaluate-rag/SKILL.md:26-39`](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/evaluate-rag/SKILL.md#L26-L39)

**Full revision:** `22418da2bfb159f28a1b0dcf64e969e14ae56c99`

**License:** [MIT, copyright Hamel Husain](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/LICENSE#L1-L21).

**Delta:** The local [`rag-app-contract`](../../data/skills/rag-app-contract.md#L13) asks for source IDs and retrieval of an ingested marker. The original Postgres candidate improves storage/query engineering, and the MCP candidate evaluates tool use. Neither supplies this skill's separation of **retrieval recall/ranking quality** from **answer faithfulness and relevance**. The source defines Recall@k, Precision@k, MRR/NDCG and two-hop retrieval checks, and treats chunking as an independently evaluated choice ([metrics](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/evaluate-rag/SKILL.md#L70-L115), [chunking](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/evaluate-rag/SKILL.md#L117-L136), [generation and multi-hop](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/evaluate-rag/SKILL.md#L138-L169)).

**Missing inputs/tools and limits:** Needs a stable corpus/chunk-ID snapshot, recorded retrieved results and independently identified relevant chunks. Start with manually curated questions; the source explicitly offers that path, so no hosted model, embedding service or paid account is required for the metric advice. Its synthetic-question generation is optional and excluded from immediate curation. Metric calculation can use the project's existing test runner. Answer-grounding judgments require human review or an independently calibrated, already-approved judge; a correct citation ID alone does not prove its cited text supports the answer. The skill does **not** ship a ready-to-use citation-entailment verifier.

**Suggested advisory outline - original paraphrase:**

1. Retain the existing marker smoke test, then add a small frozen set of queries, relevant chunk IDs and supporting answer spans, including plausible distractors.
2. Score retrieved IDs before scoring generated prose: coverage for the first retriever, ranking quality for reranking, and all-required-chunk coverage for multi-hop questions. Record the corpus version and chosen k.
3. Check emitted source IDs against retained evidence; separately assess whether each factual claim is supported and whether the answer addresses the question. Treat unsupported questions as a separate abstention case, not a divide-by-zero retrieval score.
4. Change chunking/retrieval settings one at a time and compare on unchanged evaluation examples. Keep generation failures distinct from retrieval misses; never infer success from answer similarity alone.

Items such as explicit citation-ID validation and abstention cases are **proposed SkyN3t adaptations**, not claims of an upstream packaged implementation. Restrict this advice to retrieval-bearing builds and their review/proof work.

## 2. Native Swift async and state proof

**Exact source:** [`AvdLee/Swift-Testing-Agent-Skill:swift-testing-expert/SKILL.md:12-35`](https://github.com/AvdLee/Swift-Testing-Agent-Skill/blob/798e9b1a2bcac164d4f0c781908199e754f0bab6/swift-testing-expert/SKILL.md#L12-L35)

**Full revision:** `798e9b1a2bcac164d4f0c781908199e754f0bab6`

**License:** [MIT, copyright Antoine van der Lee](https://github.com/AvdLee/Swift-Testing-Agent-Skill/blob/798e9b1a2bcac164d4f0c781908199e754f0bab6/LICENSE#L1-L21).

**Delta:** SkyN3t explicitly supports native macOS Swift/SwiftPM stacks ([registry](../../skyn3t/core/stacks.py#L81-L96)); current desktop advice includes [editor layout](../../data/skills/desktop-editor-layout.md#L13-L35), while the original nine add web/mobile proof tooling rather than Swift test semantics. This focused author-owned skill contributes awaited callbacks/event counts, fresh state under parallel execution, targeted actor isolation, and a deliberate boundary between Swift unit/integration tests and XCTest-only UI/performance scenarios ([async reference](https://github.com/AvdLee/Swift-Testing-Agent-Skill/blob/798e9b1a2bcac164d4f0c781908199e754f0bab6/swift-testing-expert/references/async-testing-and-waiting.md#L56-L81), [isolation reference](https://github.com/AvdLee/Swift-Testing-Agent-Skill/blob/798e9b1a2bcac164d4f0c781908199e754f0bab6/swift-testing-expert/references/parallelization-and-isolation.md#L39-L88), [XCTest boundary](https://github.com/AvdLee/Swift-Testing-Agent-Skill/blob/798e9b1a2bcac164d4f0c781908199e754f0bab6/swift-testing-expert/references/migration-from-xctest.md#L7-L15)).

**Missing tools/version gates:** Require a project test target and a toolchain providing `Testing`. Swift's own documentation confirms inclusion in **Swift 6 / Xcode 16**, with no added package dependency needed; newer APIs still require checking the actual toolchain ([Swift-maintainer evidence, pinned `4fbd3dfa4b565279dfae3fa94b88d5ba24fabaab`](https://github.com/swiftlang/swift-testing/blob/4fbd3dfa4b565279dfae3fa94b88d5ba24fabaab/README.md#L128-L146)). Use the installed matching toolchain, not Swift Testing's development branch. GUI lifecycle proof still requires macOS/Xcode and an appropriate XCTest UI target; SwiftPM model tests do not prove a real app launched, displayed a window or quit. No paid service or new browser/native-control tool is prescribed.

**Suggested advisory outline - original paraphrase:**

1. Identify model/controller behavior that can be proved without UI automation; give every case fresh state and explicit preconditions.
2. Await work to completion, bridge callbacks carefully and assert actual event counts before the test scope exits. Avoid sleeps as the synchronization mechanism.
3. Exercise relevant start/stop, cancellation and persistence transitions through the app's existing testable interfaces; isolate backing state and use the main actor only where required.
4. Preserve separate XCTest UI/performance coverage. Report model tests, real integration tests and GUI lifecycle evidence separately; do not label a missing GUI probe as passed.

Lifecycle scenario selection in item 3 is a **project-specific proposed adaptation**. Do not copy illustrative `#expect(true)` examples as proof, serialize the whole suite to hide shared state, or introduce known-issue annotations that waive existing delivery gates. The source is a focused independent author's skill, **not an Apple-authored SKILL.md**; platform facts above are cross-checked with the Swift project.

## 3. Calibrating a faithfulness/relevance judge

**Exact source:** [`hamelsmu/evals-skills:skills/validate-evaluator/SKILL.md:1-28`](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/validate-evaluator/SKILL.md#L1-L28)

**Full revision:** `22418da2bfb159f28a1b0dcf64e969e14ae56c99`

**License:** [MIT](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/LICENSE#L1-L21).

**Delta:** `evaluate-rag` decides what to measure; this skill checks whether an **LLM-based measuring instrument itself** agrees with human judgments. That is different from the original constraint candidate's protection against weakened gates, or generic adversarial review. It separates prompt examples, development data and untouched test data; measures recognition of both human passes and human failures; and requires revalidation after judge changes ([splits](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/validate-evaluator/SKILL.md#L32-L42), [TPR/TNR](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/validate-evaluator/SKILL.md#L62-L85), [held-out measurement](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/validate-evaluator/SKILL.md#L120-L124), [versioning](https://github.com/hamelsmu/evals-skills/blob/22418da2bfb159f28a1b0dcf64e969e14ae56c99/skills/validate-evaluator/SKILL.md#L200-L205)).

**Missing inputs/tools and limits:** Requires independently human-labeled examples of both outcomes and an already-approved runnable judge; no specific hosted provider is required by the method. Without those inputs, retain this as advisory and do not claim calibrated grading. The source illustrates calculations with scikit-learn/NumPy and an optional helper package; none is installed by the importer. Exclude its automatic percentage targets, sample-size prescriptions and aggregate bias-correction/bootstrap code from the initial compact advisory. They require a statistical design appropriate to the real dataset, not blanket adoption.

**Suggested advisory outline - original paraphrase:**

1. Specify one observable failure criterion and independently label representative passing and failing examples. Do not use the judged model's own verdict as ground truth.
2. Keep prompt examples, tuning cases and final evaluation cases disjoint; preserve a record of the split.
3. Report false passes and false failures separately with class counts and uncertainty, inspect disagreements, and choose acceptance criteria before opening the held-out set.
4. Record the exact judge model, prompt and configuration. Recalibrate after changes; never let a probabilistic judge overrule a failed deterministic citation-ID, schema or delivery check.

This is **not** a replacement for unit-testing deterministic evaluators; upstream explicitly excludes that use in its frontmatter.

## Source/adoption observations

| Source | Repository status | Stars observed | Skill installs observed | Inspected commit time |
| --- | --- | ---: | --- | --- |
| [hamelsmu/evals-skills](https://api.github.com/repos/hamelsmu/evals-skills) | Public, archived, not disabled | 1,664 | [`evaluate-rag`: 700](https://skills.sh/hamelsmu/evals-skills/evaluate-rag); [`validate-evaluator`: 608](https://skills.sh/hamelsmu/evals-skills/validate-evaluator) | 2026-08-16 05:28:24Z |
| [AvdLee/Swift-Testing-Agent-Skill](https://api.github.com/repos/AvdLee/Swift-Testing-Agent-Skill) | Public, unarchived, not disabled | 446 | [`swift-testing-expert`: 4.6K displayed](https://skills.sh/avdlee/swift-testing-agent-skill/swift-testing-expert), rounded | 2026-04-22 18:12:15Z |
| [ai-evals-course/evals-skills](https://api.github.com/repos/ai-evals-course/evals-skills) | Public, unarchived, not disabled; license unresolved in inspected sources | 556 | Not used for the historical recommendations | 2026-08-31 23:55:49Z |

Repository push time is not the inspected skill's modification time. Counters belong to the specific directory listing/source shown; none was transferred from the archived source to its successor.

## Bounded exclusions and still-open gaps

| Investigated source | Why it was not added |
| --- | --- |
| [LangSmith evaluator, `e8f4120a876b80ced98bce1bb21d6b9f4d62cdb8`](https://github.com/langchain-ai/langsmith-skills/blob/e8f4120a876b80ced98bce1bb21d6b9f4d62cdb8/config/skills/langsmith-evaluator/SKILL.md#L10-L55) | Actual skill assumes authenticated LangSmith tooling and includes remote installer/setup actions; no explicit license was identified in the inspected repository/skill. Wrong default for this bounded offline-first addition. |
| [LangChain eval engineering, `b7a2a8fc363d1711456f83d24230535c9fff93eb`](https://github.com/langchain-ai/langchain-skills/blob/b7a2a8fc363d1711456f83d24230535c9fff93eb/config/skills/eval-engineering/SKILL.md#L8-L37) | Harbor task/environment construction and project knowledge generation are broader than RAG quality measurement; licensing was unresolved. The [README](https://github.com/langchain-ai/langchain-skills/blob/b7a2a8fc363d1711456f83d24230535c9fff93eb/README.md#L89-L104) introduces additional Harbor/Docker or cloud assumptions. |
| [Postman `api-builder`, `3701e58b7ae6f60ac9e6b7f7156bb73e6e9c84eb`](https://github.com/postmanlabs/skills/blob/3701e58b7ae6f60ac9e6b7f7156bb73e6e9c84eb/plugins/postman/skills/api-builder/SKILL.md#L10-L27) | Useful contract-first intent, but binds to Postman initialization, CLI/workspace artifacts and self-update instructions; no explicit reuse license was identified. Do not mistake this for a tool-independent contract-testing skill. |
| [Schemathesis tree, `f657beff6df77a1b04e9c69915c31ef36796c193`](https://github.com/schemathesis/schemathesis/tree/f657beff6df77a1b04e9c69915c31ef36796c193) and [Ragas tree, `298b68274234c060deacab3cf5fb52aa3a20e885`](https://github.com/vibrantlabsai/ragas/tree/298b68274234c060deacab3cf5fb52aa3a20e885) | No actual `SKILL.md` was found in the inspected primary repository trees. Their tools/docs are not invented here as installable skills. |
| Tauri/Electron maintainer skill search | A scoped GitHub code search returned no Tauri `SKILL.md` result; Electron results concerned framework PR/release/upgrade work and signing-package verification, not delivered-app lifecycle proof. These are search observations, not ecosystem-wide absence claims. |

No credible complete **Tauri/Swift/Electron launch, window interaction, shutdown and relaunch proof skill** was established in this pass. Keep that gap explicit. The inspected Swift skill covers an important model/async-testing slice only. No additional API/backend candidate cleared both the reuse/access boundary and the high-marginal-value filter.

A Swift.org shortcut returned 404; Swift prerequisite facts were instead obtained from the pinned `swiftlang/swift-testing` source cited above. Other recommendation source files and historical licenses were readable. No fetched code or skill instruction was executed.

## Curation boundary

For these additions, retain exact source path, full revision, evidence hash and license notice; quarantine the resulting small advisory first. Do not import entire packs, optional scripts, installer instructions, model calls, paid-service assumptions or agent-configuration changes. The original outline text above is deliberately tool-neutral and does not grant execution permissions. SkyN3t's existing [external-candidate promotion](../EVIDENCE_LEARNING.md#L79-L116) and [advisory-only skill model](../SWARM_SKILLS.md#L44-L60) remain authoritative. Main-session migration and the original nine imports are outside this report's scope.
