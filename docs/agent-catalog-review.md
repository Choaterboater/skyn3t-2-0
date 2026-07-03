# Agent Catalog Review For SkyN3t

Reviewed sources:

- `openai/codex-plugin-cc`
- `msitarzewski/agency-agents`
- `wshobson/agents`
- `obra/superpowers`
- Local SkyN3t code paths for agents, skills, stages, generated build verification, and UI.

## Bottom Line

These repos can help in two different ways, and mixing them would make SkyN3t worse.

1. For building SkyN3t itself, use Codex custom agents, methodology skills, review/rescue workflows, and strict verification.
2. For helping SkyN3t build better apps/sites/games, curate third-party agent profiles into compact, stage-specific guidance and validation checklists. Do not bulk-install broad persona catalogs into generated app prompts.

SkyN3t already has the right integration surface: `SkillLibrary`, `StudioRunner._skill_advice()`, `_base_payload()`, `knowledge_block()`, the existing `agent_pack` stack, `CodeAgent`, `CodeImproverAgent`, and the verification gates. The missing piece is a curated catalog normalizer plus per-stage guidance injection.

## Repo Findings

### `wshobson/agents`

Most useful source for SkyN3t's build factory. The repo has a real plugin architecture with plugin manifests, agents, skills, commands, Codex adapters, generated artifacts, and structural validators.

High-value pieces:

- Plugin shape: `plugins/<capability>/{agents,skills,commands}`.
- Codex adapter ideas: Markdown agent profiles converted into `.codex/agents/*.toml`, skills into `.codex/skills`, and marketplace manifests.
- Validation tooling: generated artifacts are parsed and checked instead of trusted.
- Useful agent profiles: UI designer, frontend developer, backend architect, debugger, test automator, security auditor, deployment validation, SEO, plugin evaluation.
- Useful factory idea: tier models by risk. Use expensive/deeper models for architecture, hard debugging, security, and complex game logic; cheaper/fast models for boilerplate, SEO variants, asset labels, and simple repairs.

What to avoid:

- Do not copy the whole marketplace. SkyN3t should stay vertical around generating working apps/sites/games.
- Do not inject full long persona files into prompts. Normalize them into compact role contracts.
- Do not copy framework claims blindly. Some profile text will age.

### `agency-agents`

Useful as a broad role/persona mine, not as an execution framework to dump into builds.

High-value roles to curate:

- `engineering/frontend-developer`
- `engineering/backend-architect`
- `engineering/code-reviewer`
- `engineering/minimal-change-engineer`
- `engineering/rapid-prototyper`
- `engineering/multi-agent-systems-architect`
- `testing/reality-checker`
- `testing/evidence-collector`
- `testing/accessibility-auditor`
- `design/ui-designer`
- `design/image-prompt-engineer`
- `design/ux-architect`
- `game-development/game-designer`
- `game-development/level-designer`
- `product/product-manager`
- `marketing/seo-specialist`
- `security/appsec-engineer`
- `specialized/agents-orchestrator`

Best use inside SkyN3t:

- Convert selected roles into stack/stage-specific guidance.
- Use testing roles to strengthen visual, runtime, and evidence gates.
- Use game roles to improve Phaser mechanics, level feel, sprite use, and playtest criteria.
- Use marketing/SEO roles only for website/content stacks.

What to avoid:

- The files are long and personality-heavy. Prompt bloat would reduce build reliability.
- Some roles are domain-specific or not relevant to app generation.
- Import must be curated and licensed/provenanced.

### `obra/superpowers`

Best for our development process, not generated app runtime.

Useful practices:

- Systematic debugging: root cause before fixes.
- Verification before completion: no success claims without fresh command output.
- TDD for SkyN3t features and bug fixes where practical.
- Subagent-driven development for planned, independent implementation tasks.
- Requesting/receiving code review before shipping meaningful changes.

Best use:

- Install or adapt as developer workflow skills.
- Encode the same principles into repo guidance and project-scoped Codex agents.
- Use the methodology to run SkyN3t improvements, especially large UI/runtime changes.

What to avoid:

- Do not ship Superpowers files, hooks, ledgers, or prompts inside generated app bundles.
- Do not force TDD for throwaway generated outputs, but do use generated tests for high-quality build profiles.

### `openai/codex-plugin-cc`

Useful only when SkyN3t development is happening from Claude Code and we want Claude to delegate to Codex.

Useful pieces:

- `/codex:review` and `/codex:adversarial-review` as external review gates.
- `/codex:rescue` as a handoff for hard debugging or implementation rescue.
- Background job/status/result pattern.
- A thin forwarding subagent model that does not reason independently before delegating.

What to avoid:

- Do not add this as a SkyN3t runtime dependency.
- Do not expose Claude/Codex commands inside generated apps.
- Be cautious with stop-hook review gates because they can create long-running loops.

## Codex Roles Added For Building SkyN3t

Project-scoped custom agents now live in `.codex/agents/`:

- `skyn3t-factory-architect`: reviews pipeline/agent/model/learning architecture.
- `skyn3t-build-debugger`: investigates failed builds and CI failures from evidence.
- `skyn3t-visual-qa`: reviews UI/generated visual quality, assets, layout, and game renderability.
- `skyn3t-agent-catalog-curator`: evaluates external agent catalogs and maps them to SkyN3t stages.
- `skyn3t-security-reviewer`: checks provider keys, previews, catalog imports, and generated artifact serving.
- `skyn3t-worker`: bounded implementation worker for scoped SkyN3t tasks.

Use these for SkyN3t development. They are not part of generated project output.

## Recommended SkyN3t Runtime Agent List

These are internal build-factory roles SkyN3t should expose or simulate via stage guidance:

1. Brief/product clarifier: turns vague asks into concrete acceptance criteria.
2. Stack/router architect: chooses site/app/game/agent_pack/mcp/rag/workflow and required gates.
3. UX/design director: domain-specific layout, components, visual tone, responsive behavior, and states.
4. Asset director: Replicate/image prompts, asset relevance, and cross-project leak prevention.
5. Frontend implementer: React/Next/Astro/static UI builds.
6. Backend/API implementer: FastAPI/Node/Next API, data contracts, auth, storage.
7. Game designer/playability agent: mechanics, controls, level feel, sprites, loop clarity.
8. Test author: profile-dependent tests before code for high-quality builds.
9. Dependency/import reconciler: package correctness and virtual import handling.
10. Runtime repair specialist: targeted code_improver loops from exact verifier output.
11. Visual QA/playtester: screenshots, responsive checks, accessibility, game playfield checks.
12. Security/secret critic: provider keys, generated auth/API risks, unsafe imports.
13. SEO/content QA: metadata, crawlability, headings, structured data, content relevance.
14. Deploy/package agent: build artifacts, run instructions, deployment readiness.
15. Learning distiller: turns concrete failures and successful repairs into reusable lessons/skills.

## Implementation Path

1. Add an `agent_catalog` normalizer.
   - Inputs: local directory or GitHub checkout.
   - Outputs: `id`, `title`, `description`, `source`, `license`, `stage`, `stacks`, `tags`, `risk`, `body`.
   - Support Markdown front matter, Codex TOML, and plugin manifests.

2. Extend `SkillLibrary`.
   - Preserve existing markdown skill import.
   - Add stack aliases for `agent_pack`, `mcp`, `rag`, and `workflow`.
   - Add stage-aware matching so architect/code/repair/verifier do not receive the same blob.

3. Inject per-stage role guidance.
   - Resolve compact catalog guidance in `StudioRunner._base_payload()`.
   - Render it separately in `knowledge_block()` as role guidance, not generic skill advice.
   - Record used catalog ids in `manifest.extra["catalog_used"]`.

4. Wire repairs.
   - `CodeImproverAgent` must receive the same role/catalog guidance as CodeAgent so repair loops do not lose constraints.

5. Strengthen `agent_pack`.
   - Parameterize the existing agent-pack directive by selected target: Codex, Claude, Cursor, OpenCode, etc.
   - Validate role uniqueness, required fields, target output, and duplicate persona risk.

6. Add UI/API.
   - Add a Skills/Agent Catalog browser.
   - Let build submission choose a role pack/catalog for app/site/game builds.
   - Keep raw prompt text behind lazy endpoints; show compact provenance and quality scorecards in lists.

7. Add safety gates.
   - No arbitrary script execution during import.
   - License/provenance recorded.
   - Secret/tool instructions stripped or quarantined unless explicitly enabled.
   - Prompt-size caps and stage-specific truncation.

## Immediate Code Changes Made

- `SkillLibrary` now treats `agent_pack`, `mcp`, `rag`, and `workflow` as first-class stack alias groups. This lets imported skills or curated catalog entries match those builder types instead of being ignored as unrelated generic advice.
- `skyn3t.intelligence.agent_catalog` now safely parses Markdown and Codex TOML agent profiles into normalized catalog entries with inferred SkyN3t stages, stacks, tags, and risk level.
- The catalog parser can import normalized roles into `SkillLibrary` as compact advisory skills without executing third-party scripts or installing global agents.
- Project-scoped Codex roles were added under `.codex/agents/` for building SkyN3t itself.
