# Autonomous coding reliability: bounded recovery before broader autonomy

Date: 2026-09-16

## Findings

Prioritize a shared progress-aware attempt budget, durable preservation of
unverified candidates, and real compiler/test evidence supplied to the next
repair action. The primary sources below demonstrate mechanisms, not guaranteed
autonomy or a measured improvement in SkyN3t's free-model completion rate.

This investigation used public primary sources. It did not execute downloaded
instructions, copy third-party implementation code, or call paid models.
Recommendations are proposals unless explicitly identified as implemented.

## Verified mechanisms

### OpenHands: bounded correction, ownership fencing, bounded memory

The SDK's scripted tests assert one corrective nudge after three identical
failing calls and `STUCK` after the fourth. Another test verifies that a different
response can avoid `STUCK`. These establish control flow, not successful repair.
[Source and tests](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/tests/sdk/conversation/local/test_stuck_detector_nudge.py#L85-L153)

Its action equality includes the action's thought, arguments, and tool name;
the error streak checks matching actions and error events. This is not itself
a semantic measure of source or proof progress, and does not establish detection
of equivalent rereads with different narration.
[Action comparison](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/openhands-sdk/openhands/sdk/conversation/stuck_detector.py#L192-L215),
[error streak](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/openhands-sdk/openhands/sdk/conversation/stuck_detector.py#L325-L342)

A generation-fenced lease primitive increments its generation when ownership
changes and checks ownership under a file lock before guarded writes. Tests
reject a live competing owner and block a stale owner after takeover. This
protects participating writes, not arbitrary tools automatically.
[Lease implementation](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/openhands-agent-server/openhands/agent_server/conversation_lease.py#L122-L164),
[guarded writes](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/openhands-agent-server/openhands/agent_server/conversation_lease.py#L187-L232),
[ownership tests](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/tests/agent_server/test_conversation_lease.py#L63-L115)

The memory loader reads compact user/project `MEMORY.md` indexes with a default
6,000-character budget. Daily logs remain available for on-demand reading.
Bounding injection does not validate the truth or usefulness of the memory.
[Memory contract](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/openhands-sdk/openhands/sdk/context/memory.py#L1-L24),
[loading implementation](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/openhands-sdk/openhands/sdk/context/memory.py#L56-L106)

Skill loading must not be assumed inert: the skill invocation implementation can
render dynamic command content and explicitly notes possible disk side effects.
[Skill invocation](https://github.com/OpenHands/software-agent-sdk/blob/3103fff8d33d9d52abd4eea18ff9a50d31de0468/openhands-sdk/openhands/sdk/tool/builtins/invoke_skill.py#L163-L172)

### SWE-agent: salvage work independently of model completion

An exhausted API retry reaches an error path that attempts to extract the
existing patch. When the runtime is dead, salvage can use the last trajectory's
stored diff if one exists. A separate test confirms that a cost-limit exit can
retain a submission. Artifact retention is not correctness evidence.
[Salvage implementation](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/agents.py#L823-L868),
[API-error path](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/sweagent/agent/agents.py#L1187-L1192),
[retention test](https://github.com/SWE-agent/SWE-agent/blob/3ea751c087f32b16e039a2233dd6eefecef325d5/tests/test_agent.py#L191-L208)

Transfer the salvage principle, not autosubmission as delivery approval.

### Aider: real diagnostics become the next repair message

After edits, enabled lint/test checks can set `reflected_message` to the actual
errors, subject to confirmation. The next iteration sends that message to the
model, while a reflection counter limits further iterations. The linter includes
the command, output, and relevant source context.
[Reflection loop](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L924-L944),
[lint/test feedback](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/coders/base_coder.py#L1599-L1623),
[linter context](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/linter.py#L47-L80),
[command output](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/linter.py#L108-L116)

This is a concrete feedback path, not a guarantee that a model interprets the
diagnostics correctly.

### LangGraph: completed results survive, unfinished side effects can replay

The documentation describes retaining successful task writes when another task
fails and restoring completed task results during resume. It explicitly warns
that unfinished tasks may execute again: side effects need idempotency keys or
existing-result checks. Synchronous checkpointing persists before the next step,
trading performance for stronger durability.
[Pending writes](https://github.com/langchain-ai/docs/blob/2ab538aedbf0c323d647c236644c978192f86c39/src/oss/langgraph/checkpointers.mdx#L58-L68),
[replay/idempotency](https://github.com/langchain-ai/docs/blob/2ab538aedbf0c323d647c236644c978192f86c39/src/oss/langgraph/functional-api.mdx#L790-L810),
[synchronous persistence](https://github.com/langchain-ai/docs/blob/2ab538aedbf0c323d647c236644c978192f86c39/src/oss/langgraph/checkpointers.mdx#L599-L603)

Checkpointing alone is neither exactly-once execution nor filesystem ownership.
SkyN3t need not adopt the framework to adopt these contracts.

### Anthropic: bounded feature scope and outcome-based evaluation

The first-party autonomous-coding example instructs each fresh session to read
history, verify existing behavior, and complete one selected feature. Those are
prompt-level instructions rather than enforcement. Its runner also permits
unlimited iterations and fresh-session retries, which should not be copied into
a bounded free-provider executor.
[Coding-session instructions](https://github.com/anthropics/claude-quickstarts/blob/8826387af1d23280996f0a0892e0cfd764becb57/autonomous-coding/prompts/coding_prompt.md#L6-L74),
[iteration policy](https://github.com/anthropics/claude-quickstarts/blob/8826387af1d23280996f0a0892e0cfd764becb57/autonomous-coding/agent.py#L97-L118),
[session retry behavior](https://github.com/anthropics/claude-quickstarts/blob/8826387af1d23280996f0a0892e0cfd764becb57/autonomous-coding/agent.py#L144-L185)

First-party evaluation guidance distinguishes agent claims from environmental
outcomes, recommends deterministic graders where practical, warns about shared
state and infrastructure contamination, and recommends positive and negative
trigger cases. These principles support evaluation-gated skill changes, but do
not specify an automatic skill-promotion algorithm.
[Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)

## Ranked recommendations for SkyN3t

| Rank | Proposed mechanism | Tradeoff and invariant |
|---|---|---|
| 1 | Share physical-request, deadline, and repair budgets across provider and orchestrator layers. Fingerprint reads by normalized arguments and source version, not narration. | Legitimate exploration needs room; allow bounded novel reads, then one corrective nudge. Changing models must not replenish the budget. |
| 2 | Preserve the latest unverified candidate and the last proof-passing candidate independently of provider success. Record base/source hashes, applied operations, objective, and proof status. | More crash-consistency work and storage. `candidate_unverified` or `paused_provider` must never mean delivered. |
| 3 | Run the cheapest relevant proof after a small edit batch and feed its actual command, exit status, candidate hash, and diagnostics into at most one bounded repair. | Proof has latency. Keep complete local evidence and distinguish missing tools, interrupted commands, provider failures, and code failures. |
| 4 | Fence all project mutations with one owner/generation, including proof transitions, rollback, and delivery. Persist completed phases before advancing the queue. | Every mutation path must participate; an expired timestamp alone cannot stop a stale subprocess. |
| 5 | Keep scoped, provenance-backed advisory lessons rather than injecting raw traces. Track contradictions and distinguish code evidence from provider availability. | Sparse evidence should produce few lessons. Failure-derived advice remains a hypothesis until a linked repair supports it. |
| 6 | Promote skills using frozen checks, held-out and negative-trigger cases, repeated outcomes, and versioned rollback. Start with non-executable advice. | Evaluation consumes free-provider capacity. One successful run is not proof that the selected skill caused the improvement. |

These recommendations are inferred applications of the cited mechanisms, not
claims that the upstream projects implement this exact combined design.

## Implemented increment from this investigation

The local regression reproduced three whole agentic sessions for each exhausted
429, timeout, and 503 error. A native free-model improvement then added
`TaskResult.retryable`, made the orchestrator honor it, and set it to `False` for
already-executed hosted improver failures. Native package build and focused proof
passed; the supplied tests and other recorded helper sources were unchanged.

Relevant local implementation and regression sources:

- `skyn3t/core/agent.py`: `TaskResult`
- `skyn3t/core/orchestrator.py`: `_run_with_retries`
- `skyn3t/agents/code_improver.py`: executed agentic failure result
- `tests/test_improve_agentic.py`: `test_executed_agentic_failure_is_not_replayed_by_orchestrator`

This fixes outer session replay only. It is not a shared physical-request ledger,
candidate checkpoint/resume implementation, or a complete autonomous executor.
The assistant supplied the diagnosis, acceptance test, and precise task scope;
the free model authored the three source changes.

## Next bounded increment

Implement one resumable repair transaction rather than a general executor:

1. Claim one writer and one attempt budget.
2. Reuse bounded context and perform one small edit batch.
3. Persist the candidate and run targeted proof.
4. Supply real failure evidence to at most one repair turn.
5. Retain unverified work on provider failure; deliver only after proof passes.

Acceptance cases should include repeated unchanged reads, excessive writes
without proof, 429/524/525 after a useful edit, fallback without budget reset,
compiler evidence in the next request, crash/resume without duplicate edits,
stale-writer rejection, and provider errors that do not become successful code
lessons.

Then run a capped free-only native comparison. Measure proof-passing completions,
retained candidates, physical requests, redundant reads, writes before proof,
and wall time. Report provider-blocked outcomes separately.

## Limits

The upstream mechanisms were inspected; their test suites and benchmarks were
not executed. The proposed progress metrics, lesson-quality gates, and skill
promotion lifecycle remain unvalidated for SkyN3t. No reviewed source guarantees
autonomy or eliminates model-quality and free-provider rate-limit constraints.
