# Repo Consolidation: claude-skills + sagaflow + swarmd → 2 repos

**Date:** 2026-04-23
**Status:** Design

## End State

Two repos:

- **claude-skills** (`~/.claude/skills/`) — all skill definitions including optional bespoke workflows
- **sagaflow** — generic runtime (interpreter + missions + shared core). Zero skill-specific knowledge.

## Architecture

### claude-skills (content + optional skill code)

```
~/.claude/skills/
  deep-qa/
    SKILL.md                         # Claude Code instructions
    prompts/                         # DRY prompt templates
      critic.system.md
      critic.user.md
      ...
    __init__.py                      # register() + _build_input()
    workflow.py                      # bespoke Temporal workflow
  ccr-run/
    SKILL.md                         # simple skill, no workflow.py
  _shared/
    execution-model-contracts.md     # shared patterns
```

Each skill is a self-contained package. `workflow.py` and `__init__.py` are OPTIONAL — skills without them use the generic interpreter. Skills with them get bespoke Temporal execution.

`workflow.py` imports from sagaflow (dataclasses, retry policies) — this is the plugin-depends-on-framework direction. Normal.

### sagaflow (generic runtime)

```
sagaflow/
  core/
    temporal_client.py               # connect, preflight
    transport/                       # Anthropic SDK, Claude CLI
    retry_policies.py                # shared retry constants
    paths.py                         # ~/.sagaflow/ layout
    inbox.py                         # INBOX.md read/write
    notify.py                        # desktop notifications
    hook.py                          # Claude Code SessionStart hook
  durable/
    activities.py                    # write_artifact, emit_finding, spawn_subagent
  generic/
    tools.py                         # Anthropic tool schemas
    activities.py                    # call_claude_with_tools, tool adapters
    workflow.py                      # ClaudeSkillWorkflow + SubagentWorkflow
  missions/                          # absorbed from swarmd
    workflow.py                      # MissionWorkflow
    state.py                         # MissionState, CriterionState
    specialists/                     # PatternDetector, LLMCritic, ResourceMonitor
    activities/                      # check_criterion, completion_judge, anti-cheat, etc.
  prompts.py                         # load_claude_skill_prompt, claude_skills_dir
  registry.py                        # SkillRegistry (populated by dynamic discovery)
  worker.py                          # dynamic discovery + unified worker
  cli.py                             # sagaflow launch|mission|list|inbox|worker|doctor
```

### Dynamic Discovery (worker.py)

At startup, the worker scans `claude_skills_dir()` for skills with `__init__.py`:

```python
def build_registry() -> SkillRegistry:
    registry = SkillRegistry()
    skills_root = claude_skills_dir()
    for skill_dir in skills_root.iterdir():
        init_py = skill_dir / "__init__.py"
        if init_py.exists():
            module = importlib.import_module_from_path(init_py)
            module.register(registry)
    # Always register the generic interpreter as fallback
    register_generic(registry)
    return registry
```

No hard-coded skill imports. Adding a skill = adding files to claude-skills.

### Unified CLI

```
sagaflow launch <skill> [args] --await       # skill runtime (existing)
sagaflow mission launch <yaml>                # mission supervisor (from swarmd)
sagaflow mission status <workflow-id>
sagaflow mission abort <workflow-id>
sagaflow list                                 # all workflows (skills + missions)
sagaflow inbox                                # unread findings
sagaflow worker run                           # single worker: skills + missions
sagaflow doctor                               # temporal + transport + worker health
```

### Single Worker

One worker daemon polls one task queue. Registers:
- All bespoke skill workflows (discovered from claude-skills)
- ClaudeSkillWorkflow + SubagentWorkflow (generic interpreter)
- MissionWorkflow + observer child workflows (missions)
- All activities (shared + generic + mission)

### Migration Steps

1. **Move `skills/*/` from sagaflow → claude-skills** (10 skill directories)
2. **Replace hard-coded imports in `sagaflow/worker.py`** with dynamic discovery
3. **Create `sagaflow/missions/`** by copying swarmd's `durable/` + `schemas/`
4. **Add `sagaflow mission` CLI subcommands** mirroring swarmd's CLI
5. **Update `sagaflow/worker.py`** to register mission workflows + activities
6. **Delete `sagaflow/skills/` directory** (now in claude-skills)
7. **Retire swarmd as standalone** — it lives inside sagaflow now
8. **Tests**: move skill tests alongside skills in claude-skills, or keep in sagaflow as integration tests
9. **Reinstall + verify** all 11 skills + mission mode

### Dependency Direction

```
claude-skills/deep-qa/workflow.py
    imports → sagaflow.durable.activities (dataclasses)
    imports → sagaflow.durable.retry_policies (constants)
    imports → temporalio (workflow, activity decorators)

sagaflow
    imports → temporalio (runtime)
    imports → anthropic (SDK transport)
    discovers → claude-skills at runtime (never imports by name)
```

Plugin depends on framework. Framework discovers plugins. Clean.

### What Gets Deleted

- `sagaflow/skills/` directory (10 subdirectories) → moved to claude-skills
- `swarmd/` standalone repo → absorbed into `sagaflow/missions/`
- Duplicate modules in swarmd that sagaflow already has: `retry_policies.py`, `paths.py`, `worker.py`, `cli.py`

### What Doesn't Change

- claude-skills `SKILL.md` + `prompts/*.md` structure
- `sagaflow launch <skill>` CLI contract
- `~/.sagaflow/` directory layout (INBOX.md, runs/, worker logs)
- Temporal task queue name (`sagaflow`)
- How prompts are loaded (`load_claude_skill_prompt`)

### Risk: Test Location

Tests currently live in sagaflow (`tests/skills/test_deep_qa.py`). Options:
- **A**: Move alongside skills in claude-skills (colocation)
- **B**: Keep in sagaflow as integration tests that import from claude-skills at runtime
- **Recommended**: B — tests that exercise the Temporal runtime belong with the runtime, not with the content. They import from both sagaflow and claude-skills, which is fine for integration tests.
