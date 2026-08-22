# OpenClaw + agent-core + OpenHands integration

## Responsibility split

- **OpenClaw:** user-facing gateway and skill entry point.
- **Temporal (production durable mode):** sequences durable stages with retries/heartbeats. PostgreSQL is required by default so every worker shares the same goal state.
- **CrewAI Flow (optional orchestration mode):** can sequence the same existing goal stages for experimentation/development. It is an alternative orchestrator, not a second state machine layered on top of Temporal.
- **Existing agent-core:** remains the source of truth for goal states, scope, review, complexity, verification, metrics, and final apply authorization.
- **OpenHands:** coding worker that edits only an isolated workspace. Integrated runs default to `DockerWorkspace`.

## Why orchestration layers stay thin

The project already has a deterministic state machine. Replacing it would duplicate logic and reintroduce state/scope bugs. Both `orchestration/crewai_flow.py` and `orchestration/temporal_flow.py` therefore call the existing deterministic stages rather than implementing competing security/state rules.

Temporal and CrewAI are selectable orchestration modes. The production durable path is Temporal; CrewAI remains useful when its Flow features are specifically desired.

## Environment separation

Keep CrewAI and OpenHands in separate Python environments. This avoids dependency collisions and preserves the dedicated OpenHands environment.

CrewAI environment:

```powershell
py -m venv .venv-crewai
.\.venv-crewai\Scripts\python -m pip install -r requirements-crewai.txt
```

OpenHands environment:

```powershell
py -3.12 -m venv .venv-openhands
.\.venv-openhands\Scripts\python -m pip install -r requirements-openhands.txt
```

Reliability/Temporal environment installs `requirements-reliability.txt`.

## MVP OpenHands-only routing

The integrated MVP path defaults to OpenHands for every approved code-modification
goal. Complexity is still calculated and persisted for observability, but a
`HARD` or `CRITICAL` assessment no longer diverts the run to manual Codex.

OpenHands receives a terminal only inside its isolated workspace so it can run
tests, start local services, verify endpoints, and iterate before returning its
patch. The iteration budget is independent of the verification timeout:

```dotenv
AGENT_ARMY_OPENHANDS_ONLY=true
AGENT_ARMY_OPENHANDS_MAX_ITERATIONS=10000
AGENT_ARMY_OPENHANDS_TERMINAL_ENABLED=true
AGENT_ARMY_OPENHANDS_STUCK_DETECTION=false
```

The older `AGENT_ARMY_FORCE_OPENHANDS`, `AGENT_ARMY_CODEX_ENABLED`,
`AGENT_ARMY_COMPLEXITY_GATE_ENABLED`,
`AGENT_ARMY_REQUIRE_CODEX_FOR_COMPLEXITY`, and
`AGENT_ARMY_BUILDER_MAX_ITERATIONS` names remain accepted for existing local
MVP configurations. Set `AGENT_ARMY_OPENHANDS_ONLY=false` explicitly only when
restoring the legacy complexity-to-Codex route.

## Production durable run

Start PostgreSQL + Temporal first (the provided local Compose stack is for local integration/development), then run the Temporal worker and submit work with the Temporal orchestrator.

```powershell
python run_temporal_worker.py
python run_integrated.py --repo "C:\path\to\repo" --task "Fix the requested bug" --orchestrator temporal
```

Temporal production mode fails closed unless PostgreSQL is configured. `AGENT_ARMY_TEMPORAL_REQUIRE_POSTGRES=false` is an explicit development-only opt-out.

`run_integrated.py` also performs a production preflight by default: Git, Docker daemon, isolated OpenHands interpreter, and enabled durable/gateway endpoints must be ready before a task is accepted. Disable only for low-level development with `AGENT_ARMY_PREFLIGHT_ENABLED=false`.

## Transactional apply

The normal result stops at `READY_TO_APPLY`. Apply remains an explicit authorization gate.

The final apply implementation is transactional at the Git level:

1. Create a detached worktree from the approved base commit.
2. Apply and verify the patch in that isolated worktree.
3. Commit the verified tree and persist its commit SHA.
4. Fast-forward the real repo only after verification passes.
5. Re-run final verification; if it fails, reset to the approved base commit while holding the repo lock.
6. An `APPLYING` retry resumes the persisted transaction instead of applying a second patch.

To apply from the local CLI only after explicit approval:

```powershell
python finalize_goal.py apply --goal-id GOAL-... --apply
```

`--auto-apply` exists for deliberate automation and disposable E2E fixtures; the OpenClaw bridge intentionally never forwards it from an ordinary user task.

## CrewAI mode

CrewAI remains available as an alternative thin sequencing layer:

```powershell
.\.venv-crewai\Scripts\python run_integrated.py --repo "C:\path\to\repo" --task "Fix the requested bug" --orchestrator crewai
```

## Native fallback

The same engine can be exercised without CrewAI or Temporal for diagnostics:

```powershell
python run_integrated.py --repo "C:\path\to\repo" --task "..." --orchestrator native
```

## Real-provider E2E

`.github/workflows/live-agent-e2e.yml` is a manual, disposable E2E test. It creates a temporary buggy Git repository on the GitHub runner, starts PostgreSQL + Temporal, starts the real worker, invokes the real Gemini/OpenHands chain, uses transactional apply on the disposable repo, and asserts the final tests pass. It requires `GEMINI_USER_A_KEY`, `GEMINI_USER_B_KEY`, `GEMINI_USER_C_KEY`, and `GEMINI_USER_D_KEY` as GitHub Actions secrets. It never targets a user repository.
