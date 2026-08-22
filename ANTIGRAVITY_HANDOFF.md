# Antigravity Development Handoff

## Purpose

Continue development of the `Piardian/orkestrat-r` orchestrator without restarting the architecture or losing the MVP decisions already made.

Read this file before planning or modifying code.

## Active development branch

`fix/integrated-openhands-mvp`

Do not develop directly on `main`. Inspect `git status` and preserve all existing user changes before editing.

## Product direction during MVP

- The integrated pipeline is the product under development.
- The required chain is Commander -> Analysts -> Reviewer -> OpenHands Builder -> verification -> apply.
- OpenHands is the only implementation executor during the MVP.
- Do not restore Codex/manual fallback or route high-complexity goals away from OpenHands.
- Complexity assessment may produce evidence, but it must not block OpenHands execution.
- Long-running Builder tasks must not use the old 12-iteration limit.
- Temporary application-level scope restrictions may be bypassed only through the explicit `--mvp-unrestricted` flag.
- Do not remove real functional verification merely to make a pipeline state pass.

## Verified milestone

On 2026-08-22, the real native integrated Test 9 completed with:

- State: `COMPLETED`
- Applied: `YES`
- Orchestrator: `native`
- Builder: OpenHands in Docker
- The broken Node.js invoice implementation was fixed and applied.
- `npm test` passed.
- `/health` and `/demo` verification passed.

This was an actual `run_integrated.py` execution, not the separate direct-demo runner.

## Important recent fixes

1. OpenHands-only MVP routing redirects legacy `CODEX_REQUIRED` goals to OpenHands.
2. Integrated runs default to Docker-isolated OpenHands and Docker verification.
3. Docker verification commands use a persistent container lifecycle so a background server remains available for later curl commands.
4. Windows command input sent to Docker uses LF-only input.
5. OpenHands iteration limits are configurable through:
   - `AGENT_ARMY_OPENHANDS_MAX_ITERATIONS`
   - `AGENT_ARMY_BUILDER_MAX_ITERATIONS`
6. `run_integrated.py --mvp-unrestricted` explicitly enables temporary MVP permission bypasses while preserving the integrated agent stages and functional verification.
7. The unrestricted mode bypasses application-level dirty-worktree preflight and file-scope/policy blockers. Patch integrity, Git applicability, functional verification, and post-apply verification remain meaningful completion requirements.

## Runtime configuration used for local native MVP tests

The local Windows test setup uses a filesystem state backend rather than PostgreSQL:

```text
AGENT_ARMY_STATE_BACKEND=file
AGENT_ARMY_DATABASE_URL=
AGENT_ARMY_TEMPORAL_REQUIRE_POSTGRES=false
AGENT_ARMY_PREFLIGHT_ENABLED=false
AGENT_ARMY_ORCHESTRATOR=native
AGENT_ARMY_OPENHANDS_ONLY=true
AGENT_ARMY_OPENHANDS_MAX_ITERATIONS=2000
```

Do not commit credentials or the user's real `.env`.

## Key entry points

- `agent-core/run_integrated.py`
- `agent-core/orchestration/engine.py`
- `agent-core/goal/runtime_policy.py`
- `agent-core/goal/builder_service.py`
- `agent-core/goal/openhands_docker_hardened.py`
- `agent-core/goal/verification_sandbox.py`
- `agent-core/goal/finalize.py`

## Direct demo versus integrated pipeline

`run_demo_goal_direct.py` starts a direct OpenHands session. It is useful for proving that OpenHands can edit and test a repository, but it does not prove that Commander, Analysts, Reviewer, and the deterministic integrated state machine ran.

Only `run_integrated.py` and its persisted goal artifacts count as an integrated Agent Army acceptance test.

## Definition of a successful integrated acceptance test

A successful test must provide evidence for all of the following:

- Commander planning artifact exists.
- Analyst review artifacts exist.
- Reviewer verdict exists.
- OpenHands actually executed.
- Builder verification passed.
- Final review passed.
- Patch was applied only when explicitly requested.
- Post-apply verification passed.
- Final state is `COMPLETED`.
- Output reports `Applied: YES` when `--auto-apply` was supplied.
- No test was deleted or weakened.
- No runtime junk or background server remains in the target repository.

## Development rules

1. Inspect before editing.
2. Do not discard unrelated or user-authored changes.
3. Keep normal mode gated; temporary permissive behavior must remain explicit.
4. Do not report agent roles as having run merely because their names appeared in a prompt. Use persisted artifacts as evidence.
5. Preserve deterministic state transitions and diagnostic JSON artifacts.
6. Run focused tests after edits, then the relevant regression suite.
7. Show the final diff and status before pushing.
8. Do not push, merge, rewrite history, or modify `main` without explicit user approval.
9. Never commit API keys, tokens, local paths containing secrets, runtime goal data, or generated server logs.

## First Antigravity task

Before modifying code:

1. Read this handoff and the repository documentation.
2. Inspect the current branch, recent commits, and working tree.
3. Inspect the recent MVP unrestricted-mode implementation.
4. Run the relevant unit tests.
5. Check the current CI status if GitHub CLI access is available.
6. Report verified current state, unresolved risks, and the next three MVP priorities.
7. Wait for user approval before implementing the next priority.
