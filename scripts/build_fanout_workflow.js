export const meta = {
  name: 'skyn3t-2-build-fanout',
  description: 'Build all remaining SkyN3t 2.0 packages in parallel against fixed core contracts, baking in the P0/P1 2.0 features',
  phases: [
    { title: 'Build', detail: 'parallel package implementation against pinned contracts' },
  ],
}

const ROOT = '/Users/stephenchoate/Documents/skyn3t 2.0'

const SUMMARY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['package', 'files', 'notes'],
  properties: {
    package: { type: 'string' },
    files: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['path', 'purpose'], properties: { path: { type: 'string' }, purpose: { type: 'string' } } } },
    tests: { type: 'array', items: { type: 'string' } },
    backlog_addressed: { type: 'array', items: { type: 'string' } },
    py_compile_clean: { type: 'boolean' },
    notes: { type: 'string' },
    todos: { type: 'array', items: { type: 'string' } },
  },
}

const PREAMBLE = [
  'You are a senior engineer building ONE package of SkyN3t 2.0, a clean-room autonomous multi-agent app factory written in async Python 3.11.',
  'The repo root is: ' + ROOT + '  (NOTE the space in the path — always quote it). A Python venv exists at .venv/ with the core deps installed.',
  '',
  '=== HARD RULES (do not violate) ===',
  '1. Write ONLY new files under YOUR assigned directory/files. Do NOT modify or create: pyproject.toml, any __init__.py, skyn3t/core/*, skyn3t/config/*, skyn3t/memory/models.py, skyn3t/memory/store.py, skyn3t/adapters/llm.py, tests/test_spine.py. Other agents own other directories — never touch theirs.',
  '2. Use FULLY-QUALIFIED imports (from skyn3t.core.agent import BaseAgent). Never edit __init__.py files; they stay empty.',
  '3. Guard every optional heavy dependency (chromadb, sentence_transformers, tree_sitter, fastapi, uvicorn, docker, prometheus_client, playwright, telegram, discord, slack_sdk) in try/except ImportError so the module ALWAYS imports even when the dep is absent. Degrade gracefully (design rule #6).',
  '4. Modules must import with ZERO side effects and ZERO network/disk writes at import time.',
  '5. Code must be real and functional — not placeholder stubs. Where a feature needs an unavailable dep or API key, provide a working degraded path (e.g., in-memory fallback, deterministic offline behavior).',
  '6. After writing, run for EACH file: "' + ROOT + '/.venv/bin/python" -m py_compile "<file>"  and fix any syntax error until clean. Do NOT run pip install. Do NOT run the full pytest suite (sibling packages may not exist yet).',
  '7. Add at least one offline pytest under tests/ named test_<yourpackage>.py that needs no network and no heavy deps (skip with pytest.importorskip if a heavy dep is required).',
  '8. Keep the 7 design rules true: (1) delivered != empty, (2) close every learning edge, (3) verify behavior not vibes, (4) safe by default, (5) cheap by default, (6) degrade dont crash, (7) everything is an event.',
  '',
  '=== PINNED CONTRACTS (import and use exactly these; they are implemented and tested) ===',
  'CONFIG  from skyn3t.config.settings import Settings, get_settings, REPO_ROOT',
  '  get_settings() -> cached Settings. Fields incl: data_dir/projects_dir/logs_dir/vector_db_path (Path), db_url, host, port, auth_token, openrouter_api_key, anthropic_api_key, openai_api_key, kimi_api_key, free_only(bool), no_claude(bool), model_evolution, openrouter_max_concurrency, per_build_usd_cap, daily_usd_cap, daily_token_cap, autonomous_daily_build_cap, debate_enabled, reflective_retry, auto_route, best_of_n(int), critic_enabled, visual_self_heal, reward_hardening, autonomous_builds, autonomous_learning, approval_gates, cortex_auto_approve_safe, execution_backend(auto|docker|inline), sandbox_hardening, sandbox_drop_caps, learnings_dir, skills_hub_paths. Props: claude_available, has_any_llm.',
  'EVENTS  from skyn3t.core.events import Event, EventBus, EventType',
  '  EventType members: TASK_SUBMITTED/ROUTED/STARTED/COMPLETED/FAILED/RETRYING, AGENT_REGISTERED/UNREGISTERED/HEARTBEAT/FAILED/RECOVERED, KNOWLEDGE_UPDATED, INSIGHT_PUBLISHED, LESSON_CAPTURED, BUILD_STARTED/STAGE_STARTED/STAGE_COMPLETED/COMPLETED/FAILED, PROPOSAL_CREATED/DECIDED, SYSTEM, ALL.',
  '  EventBus(): unsub=subscribe(EventType, async handler); await publish(Event); await emit(type, source, payload=None, correlation_id=None)->Event; history(limit=None, event_type=None)->list[Event]; published_count; snapshot()->dict; restore(snap).',
  '  Event(type, source, payload={}, id, timestamp, correlation_id). .to_dict().',
  'AGENT  from skyn3t.core.agent import BaseAgent, AgentCapability, AgentStatus, TaskRequest, TaskResult',
  '  BaseAgent(name, agent_type, provider, event_bus, config=None): add_capability(AgentCapability(name,description,tags)); has_capabilities(tuple); await start(); await run(task)->TaskResult; abstract async initialize(), async execute(task)->TaskResult, async health_check()->bool. self.status, self.metadata, self.config.',
  '  TaskRequest(type, payload={}, capabilities_required=(), priority=5, task_id, correlation_id, idempotency_key, max_retries=2, metadata={}).',
  '  TaskResult(task_id, success, output={}, error=None, agent_name=None, duration_ms=0.0, attempts=1, metadata={}).',
  'ORCHESTRATOR  from skyn3t.core.orchestrator import Orchestrator, SelfHealingManager, classify_error',
  '  Orchestrator(event_bus, max_concurrency=8, persist=None): await register(agent); await unregister(name); .agents (dict); attach_self_healing(mgr); await submit(task)->TaskResult; await wait_for_result(id,timeout=30); await health_report().',
  'ROUTER  from skyn3t.core.model_router import ModelRouter, Tier   (Tier: CHEAP, UI, BACKEND, STRONG, DOCS). resolve(tier, file_hint=None)->str; describe().',
  'MEMORY  from skyn3t.memory.store import MemoryStore  -> await init_db(); await save_task(task,result); await recent_tasks(); await save_build(**fields with build_id,slug,brief,stack,status,score,verdict,cost_usd,artifact_dir,manifest); await recent_builds(); await add_lesson(stack,stage,text,source_build=None)->int; await relevant_lessons(stack,stage="",limit=5); await grade_lesson(id,helpful:bool).',
  '  models: from skyn3t.memory.models import Base, AgentRow, TaskRow, MessageRow, LessonRow, BuildRow, KnowledgeDocRow.',
  'LLM  from skyn3t.adapters.llm import LLMClient, LLMResult, BudgetTracker, BudgetExceeded',
  '  LLMClient(settings=None, router=None): .backend ("openrouter"|"stub"); await complete(prompt, tier=Tier.CHEAP, system=None, file_hint=None, max_tokens=2048, json_mode=False)->LLMResult(text,model,backend,prompt_tokens,completion_tokens,cost_usd). .budget.',
  '',
  '=== CANONICAL STAGE VOCABULARY (agents register with these agent_type AND a same-named capability; studio submits TaskRequest(type=agent_type, capabilities_required=(capability,))) ===',
  '  brainstorm->brainstorm, research->research, architect->architecture, designer->design, code->codegen, code_improver->code_improve, reviewer->review, critic->critique, writer->documentation,',
  '  contract_verifier->verify_contract, build_verifier->verify_build, boot_verifier->verify_boot, consistency_reviewer->verify_consistency, integration_verifier->verify_integration,',
  '  test_author->test_author, packaging->package, deploy->deploy, browser->browser, github_explorer->github_explore, github_ingestor->github_ingest.',
  '  Stage payload convention (read defensively with .get): brief, slug, project_dir, worktree_dir, stack, plan, prior (dict of prior stage outputs). Return stage output in TaskResult.output (e.g. reviewer -> {"score": float 0-100, "verdict": "go"|"no_go", "gaps": [...]}; code -> {"files_written": int, "worktree_dir": str}).',
  '',
].join('\n')

const SPECS = [
  ['studio', 'skyn3t/studio/', [
    'Build the StudioRunner brief->app pipeline. Files: skyn3t/studio/manifest.py (BuildManifest dataclass + JSON persist per project), skyn3t/studio/planner.py (Planner: brief -> ordered list of stage specs + detected stack; supports an optional test-first ordering when settings.best_of_n or a test_first flag is set), skyn3t/studio/stages.py (StageSpec dataclass + default pipeline definitions brainstorm->research->architect->design->code->verify(contract,build,boot)->critic->review->package), skyn3t/studio/approval_gate.py (ApprovalGate: pause/resume on gated stages, in-memory pending store, approve/reject), skyn3t/studio/clarification.py (turn an ambiguous brief into clarifying questions; auto-answer with defaults when unattended), skyn3t/studio/proof_run.py (objective proof: run install+build+boot smoke via the sandbox/execution backend or a degraded local check; never trust an empty scaffold), and skyn3t/worktree.py (git worktree helper: create isolated worktree, and CRITICALLY merge_back(worktree_dir, project_dir) that copies generated files INTO the delivered project — design rule #1 delivered != empty).',
    'StudioRunner.start(brief, slug=None, extra=None) is async: emits BUILD_STARTED, plans stages, for each stage submits TaskRequest to the orchestrator (type=agent_type, caps=(capability,)) inside a worktree, saves output to prior, runs verifiers, applies the Critic gate (skip if settings.critic_enabled is False), computes a reviewer score, MERGES worktree output back into PROJECTS_DIR/<slug>/, writes the manifest + artifacts, saves a BuildRow via MemoryStore, emits BUILD_COMPLETED. Must run end-to-end OFFLINE (stub agents may be absent -> if a stage has no agent, record a skipped stage and continue, never crash). Inject relevant lessons (MemoryStore.relevant_lessons) into stage payloads and grade them after (close the learning loop).',
    '2.0 P0 BEST-OF-N: when settings.best_of_n>1, run the code stage N times in parallel worktrees, run the build/boot verifier on each candidate, and SELECT the best-passing trajectory (fall back to the most-complete). Add studio/best_of_n.py for the sampler+selector. 2.0 P1 test-first: planner can place test authoring before code.',
  ].join('\n'), ['Best-of-N trajectory sampling (P0)', 'Test-first build mode (P1)', 'File-level plan checklist (P2)']],

  ['agents_generative', 'skyn3t/agents/ (brainstorm.py, research_agent.py, architect.py, designer.py, code_agent.py, code_improver.py, writer.py)', [
    'Implement the generative agents, each subclassing BaseAgent, each using LLMClient with an appropriate Tier (brainstorm/research=CHEAP, architect=STRONG, designer=UI, code=per-file via file_hint, writer=DOCS). Each execute() reads task.payload (brief, plan, prior, worktree_dir) and returns structured output.',
    'CodeAgent is the most important: it writes actual source files into task.payload["worktree_dir"]. With a real LLM backend it generates from prompts; with the OFFLINE stub backend it MUST still emit a real, runnable minimal scaffold for the detected stack (e.g. a working Vite+React counter app: package.json, index.html, src/main.jsx, src/App.jsx, vite.config.js) so an offline `skyn3t studio build` yields a genuinely runnable project (design rule #1). Detect stack from the plan/brief; default to react_vite. Repair common entrypoint import/export mismatches before returning.',
    'CodeImprover takes existing files + reviewer gaps and rewrites them. Keep all agents offline-safe.',
  ].join('\n'), ['Pre-coding research module (P2)']],

  ['agents_review_verify', 'skyn3t/agents/ (reviewer.py, critic.py, contract_verifier.py, build_verifier.py, boot_verifier.py, consistency_reviewer.py, integration_verifier.py, test_author.py)', [
    'Implement verification + review agents. ReviewerAgent scores a project 0-100 with a heuristic blend (has entrypoint, non-empty files, manifest present, build config valid) optionally blended with an LLM score; returns {"score","verdict":"go"|"no_go","gaps":[...]}. Verifiers run real checks (contract: required files exist; build: npm/pip install+build via execution backend or degraded dry check; boot: smoke import/serve; consistency: cross-file import sanity; integration: wiring between frontend/backend).',
    '2.0 P0 CRITIC: critic.py is a DEDICATED ADVERSARIAL reviewer (distinct from ReviewerAgent) that hunts bugs/security/edge-cases in the generated diff and can BLOCK delivery; uses Tier.STRONG; returns {"blocking_issues":[...],"verdict":"pass"|"block"}.',
    '2.0 P0 REWARD HARDENING: add a module-level helper in build_verifier.py named detect_reward_hacking(artifact_dir, claimed) that flags gamed graders: empty/trivial tests, fabricated success logs, deleted assertions, suspiciously-tiny projects claiming success. Verifiers must require minimum real content before reporting green (design rule #3).',
    '2.0 P1 TEST-FIRST: test_author.py authors a verification suite from the brief BEFORE implementation so success is machine-checkable.',
  ].join('\n'), ['Adversarial Critic agent (P0)', 'Reward hardening / anti-reward-hacking (P0)', 'Test-first build mode (P1)', 'File-level plan as verifier checklist (P2)']],

  ['agents_external', 'skyn3t/agents/ (browser_agent.py, github_explorer.py, github_ingestor.py, stack_detector.py, env_scanner.py, packaging_agent.py, deploy_agent.py)', [
    'StackDetector.detect(dir)->family in {web,server,fullstack,unknown} from manifests. EnvScanner finds process.env.X / import.meta.env.X / os.getenv refs. PackagingAgent turns a scaffold into something a stranger can run (web: useConfig/Settings + .gitignore + README; server: Dockerfile + docker-compose + .env.example; fullstack: both).',
    'GithubExplorer/GithubIngestor LEARN FROM external repos (read-only originals): clone/fetch metadata via httpx to the GitHub API (guard: no token -> limited/disabled, never crash), assess license (SPDX allowlist), redact secrets, and surface patterns. Add assess_github_learning_source(repo) gating public/approved + license + read-only + local-candidate-copy workflow.',
    '2.0 P0 VISUAL SELF-HEAL: browser_agent.py drives a running app via Playwright if available (guard import) — navigate, click, fill, screenshot, collect console/JS errors; expose verify_rendered(url) returning {"ok":bool,"errors":[...],"screenshot":path} so the studio can use it as a verification gate. Degrade to a no-op with a clear message if Playwright is absent.',
    '2.0 P1 DEPLOY: deploy_agent.py deploys a verified scaffold to a target (static/Fly/Render/Vercel) returning a shareable URL; guard all SDKs; degrade to "deploy unavailable" cleanly. 2.0 P2: commit-history-aware ingestion in github_ingestor.',
  ].join('\n'), ['Computer-use visual self-heal loop (P0)', 'Live deploy-to-URL packaging (P1)', 'Commit-history-aware ingestion (P2)', 'Public-code provenance match (P2)']],

  ['intelligence', 'skyn3t/intelligence/', [
    'Files: learning_loop.py (capture lesson from a build outcome -> MemoryStore.add_lesson; inject relevant lessons into the next matching build; grade them by outcome — CLOSE THE LOOP, design rule #2), skill_library.py (scored skills as markdown; inject relevant skills as non-binding advice; record_use after each build; auto-promote a build pattern to a skill above ~90% over 20+ uses), build_patterns.py (scoreboard of winning build shapes per stack), debate.py (flag-gated multi-LLM debate: cheap models propose->cross-examine->vote->synthesize; record to tournament), reflection.py (analyze success/failure, publish KNOWLEDGE_UPDATED), routing_recommendations.py + model_tournament.py (record per-task model wins; recommend routing; feed model evolution), docker_backend.py (execution backend abstraction: docker if available else inline with a LOUD warning — never a silent in-process exec; design rule #4).',
    '2.0 P1 GEPA/PROMPT EVOLUTION hook: reflection.py exposes propose_prompt_improvement(agent_name, transcripts) returning a candidate instruction diff (Pareto-style: keep improvements that do not regress other tasks). 2.0 P1 auto-mined best practices from accepted vs rejected outputs in learning_loop.py.',
  ].join('\n'), ['GEPA/Pareto prompt evolution (P1)', 'Auto-mined repo best-practices (P1)', 'Population/archive evolutionary loop (P2)', 'Learned model router (P2)']],

  ['rag', 'skyn3t/rag/', [
    'Files: vector_store.py (Chroma wrapper if chromadb available, else an in-memory cosine fallback so it ALWAYS works), embeddings.py (sentence-transformers if available else a deterministic hashing embedding fallback), document_processor.py (text/markdown/code chunking), retrieval.py (hybrid semantic + BM25 if rank_bm25 available else semantic-only), rag_engine.py (ingest->chunk->embed->query->answer high-level API).',
    '2.0 P0 REPO MAP: repo_map.py builds a tree-sitter symbol graph (functions/classes/imports + relationships) of a generated/ingested codebase and exposes a token-bounded context retriever get_repo_map(dir, max_tokens). Guard tree_sitter/tree_sitter_languages imports; degrade to a regex-based symbol extractor (def/class/function/import) when absent so it ALWAYS returns a useful map. 2.0 P1 incremental Merkle/AST indexing: hash files, only re-index changed ones.',
  ].join('\n'), ['Repo map / symbol graph tree-sitter (P0)', 'Incremental Merkle/AST indexing (P1)']],

  ['memory_brain', 'skyn3t/memory/ (consciousness.py, ingestor.py, meta_agent.py, tuner.py)', [
    'consciousness.py: CollectiveConsciousness — shared working memory: KV store with TTL, sessions, agent insights; used to inject collective_context into the next task. ingestor.py: ExperienceIngestor — subscribes to TASK_COMPLETED/BUILD_COMPLETED and auto-ingests outcomes into the RAG vector store (import rag.rag_engine lazily/guarded). meta_agent.py: MetaAgent — observes system trends from MemoryStore/EventBus and generates improvement hypotheses (emit INSIGHT_PUBLISHED). tuner.py: SelfTuningEngine — listens to KNOWLEDGE_UPDATED and applies safe reflection suggestions to live agent configs.',
    '2.0 P2 MEMORY HYGIENE: add scoring, versioning, and decay to consciousness/lessons (decay old low-score entries) so memory stays clean. Do NOT modify store.py/models.py — add a hygiene helper module memory/hygiene.py instead that operates via MemoryStore public methods.',
  ].join('\n'), ['Memory hygiene: scoring/versioning/decay (P2)']],

  ['cortex', 'skyn3t/cortex/', [
    'Autonomy layer. Files: proposal_store.py (Proposal dataclass types: tuning/feature/ingest/code_patch; store + status), handlers.py (apply handlers per type), bootstrap.py (start independent components), components.py (gated_tuner, feature_suggester, curiosity_loop, review_watcher, auto_cleanup), autonomous_loop.py (autonomous build loop with daily count + budget caps + heartbeat stall detection — design rule #4/#5), repo_scout.py (scout GitHub for patterns, file ingest proposals), prompt_evolver.py.',
    'Auto-triage: duplicates rejected; system + external-ingest proposals GATED for human approval; safe ones auto-apply above a confidence threshold ONLY if settings.cortex_auto_approve_safe. Respect settings.approval_gates and settings.autonomous_daily_build_cap. Everything event-driven.',
    '2.0 P1 GUARDRAILS: autonomous_loop.py implements hard loop/budget stop with heartbeat stall detection (abort a run that makes no progress for N ticks). 2.0 P1 prompt_evolver.py drives GEPA-style base-prompt evolution as gated proposals. 2.0 P2 CI-failure ingestion + PR-comment rework hooks (event handlers that turn external CI/PR signals into repair tasks).',
  ].join('\n'), ['Loop/budget guardrails + stall detection (P1)', 'GEPA prompt evolution proposals (P1)', 'CI-failure ingestion loop (P2)', 'PR-comment rework loop (P2)']],

  ['web_api', 'skyn3t/web/ (app.py, routes.py, websockets.py, deps.py)', [
    'FastAPI app (guard fastapi/uvicorn imports — if absent, expose a create_app() that raises a clear RuntimeError only when CALLED, not at import). REST endpoints: GET /api/status, /api/agents, /api/llm/backends (router.describe + budget), /api/budget, POST /api/studio/build, GET /api/studio/builds, POST /api/studio/approve, GET /api/proposals, POST /api/proposals/decide, GET /api/skills, GET /api/knowledge/search, GET /api/metrics. WebSockets: /ws (all events), /ws/swarm (agent status), /ws/proposals. Auth: bearer token from settings.auth_token, or allow loopback when token empty.',
    'app.py wires an EventBus->WebSocket bridge (subscribe to EventType.ALL, push to connected sockets). Serve the built SPA from web/ui/dist if present (StaticFiles), else a minimal HTML status page. Provide a get_app() factory used by uvicorn and the CLI. Everything must import even if fastapi is not installed.',
  ].join('\n'), ['Trajectory replay/time-travel UI (P2 - backend hooks only)']],

  ['web_ui', 'skyn3t/web/ui/', [
    'Minimal but real Vite + React 18 + Tailwind dashboard SOURCE (no node_modules; do not run npm). Files: package.json (vite, react, react-dom, react-router-dom, @tanstack/react-query, tailwindcss, postcss, autoprefixer, three, @react-three/fiber as deps), vite.config.js, index.html, postcss.config.js, tailwind.config.js, src/main.jsx, src/App.jsx (router + nav), src/api.js (fetch wrapper + WS hook to /ws), src/styles.css (tailwind directives), and route components under src/routes/: Overview.jsx, Agents.jsx, Studio.jsx, Cortex.jsx, Brain.jsx (a simple three.js/r3f rotating brain placeholder), Skills.jsx, Activity.jsx, Settings.jsx. Pages fetch from the /api/* endpoints and subscribe to /ws. Keep it buildable (valid JSX, real imports). Add a README in web/ui explaining npm install && npm run build -> dist/.',
  ].join('\n'), ['Dashboard live swarm/pipeline/brain viz (P5)']],

  ['integrations', 'skyn3t/integrations/', [
    'Messaging + delivery. Files: channels.py (Channel ABC: async handle_inbound(msg), async send(target, text), is_available()->bool; ChannelRegistry), telegram.py, discord.py, slack.py (each guards its SDK import; no token -> is_available()=False, never crash; handle_inbound submits a brief or drives an approval), github_webhook.py (FastAPI router or plain handler that verifies signature and turns events into tasks; guard fastapi), gateway.py (DeliveryGateway: best-effort outbound to any channel, NEVER raises on an unconfigured channel — design rule #6), scheduler.py (NL-cron bridge: parse a natural-language schedule into cron, register jobs; guard any cron lib, provide a pure-python tick fallback).',
    'All credential-gated: missing key disables the feature gracefully. A brief submitted from a channel must flow to the orchestrator/studio via the event bus.',
  ].join('\n'), ['Messaging control surfaces (P7)', 'NL-cron scheduling + delivery gateway (P7)']],

  ['platform', 'skyn3t/security/ + skyn3t/observability/ + skyn3t/persistence/ + skyn3t/registry/ + skyn3t/self_healing/', [
    'security/sandbox.py (run a command in Docker by default; detect docker; if absent and EXECUTION_BACKEND=auto, fall back to a hardened local subprocess WITH A LOUD WARNING — never a silent exec; honor sandbox_hardening/drop_caps), security/secrets.py (secrets store + filter secrets out of agent env), security/audit.py (append-only audit log), security/permissions.py (capability/permission checks + live-read approval). 2.0 P1 ENV WARM-START: sandbox.py exposes warm_start_image(stack) that builds/reuses a pre-provisioned Docker image with deps installed per stack template (guard docker).',
    'observability/metrics.py (Prometheus counters/gauges if prometheus_client available else no-op registry), observability/cost_tracker.py (token+USD tracking, reads LLMClient.budget), observability/traces.py (trajectory/span traces persisted to logs), observability/health.py (HealthRegistry + doctor checks).',
    'persistence/checkpoint.py + recovery.py (snapshot/restore EventBus + key state to disk on boot; 3-mode restore: files/task/files+task). registry/catalog.py (curated integration/connector catalog the builder can wire from a brief). self_healing/budget.py (BudgetGuard hard-stop with heartbeat stall detection, complementing core SelfHealingManager).',
  ].join('\n'), ['Env warm-start snapshots (P1)', 'Loop/budget guardrails + stall detection (P1)', 'Granular 3-mode checkpoint restore (P2)', 'Connector catalog (P2)', 'Prometheus/cost/traces observability (P8)']],

  ['cli_docs', 'skyn3t/cli/main.py + README.md + STATUS.md + docs/ARCHITECTURE.md + docs/ROADMAP.md', [
    'Typer CLI (app = typer.Typer()) with commands: start (boot orchestrator + register available agents + optionally run web), doctor (health/readiness: python version, deps present, db init, llm backend, sandbox backend, projects dir writable — print a rich table), studio build "<brief>" [--best-of N] [--no-critic] (run a build end-to-end, print result + artifact path), studio approve <id> / studio reject <id>, project list (recent builds), snapshot (save state), domain ingest <path-or-url>. Use lazy imports inside each command so the CLI loads fast and tolerates missing optional packages. Wire registration by capability vocabulary.',
    'README.md: what SkyN3t 2.0 is, quickstart (python -m venv, pip install -e ".[dev]", skyn3t doctor, skyn3t studio build), architecture diagram (mermaid), how it differs (2.0 features). STATUS.md: current state + what works offline vs needs keys. docs/ARCHITECTURE.md: package map + dataflow + the 7 design rules. docs/ROADMAP.md: the 2.0 feature backlog with P0/P1/P2 and which are implemented vs planned (you will be told the backlog items in your assignment).',
  ].join('\n'), ['CLI start/doctor/studio/snapshot/domain (P8)', 'Docs + roadmap']],
]

phase('Build')
const results = await parallel(SPECS.map(([key, dir, spec, backlog]) => () =>
  agent(
    PREAMBLE +
    '\n\n=== YOUR ASSIGNMENT ===\nPackage key: ' + key + '\nAssigned directory/files: ' + dir +
    '\n2.0 backlog items you are responsible for: ' + JSON.stringify(backlog) +
    '\n\nSpec:\n' + spec +
    '\n\nWrite all files now, py_compile each until clean, add the offline test, then return your summary.',
    { label: 'build:' + key, phase: 'Build', agentType: 'general-purpose', schema: SUMMARY_SCHEMA, effort: 'medium' }
  ).then(r => ({ key, ...r }))
))

return { built: results.filter(Boolean) }
