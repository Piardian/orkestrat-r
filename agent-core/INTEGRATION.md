# OpenClaw + CrewAI + OpenHands integration

## Responsibility split

- **OpenClaw:** user-facing gateway and skill entry point.
- **CrewAI Flow:** sequences the existing goal stages. It does not own security policy.
- **Existing agent-core:** remains the source of truth for goal states, scope, review, complexity, verification, metrics, and final apply authorization.
- **OpenHands:** coding worker that edits only the isolated git worktree prepared by `GoalBuilderService`.

## Why CrewAI is a thin layer

The project already has a mature state machine. Replacing it would duplicate logic and reintroduce the exact state/scope bugs the guards were built to prevent. `orchestration/crewai_flow.py` therefore calls the existing deterministic stages rather than implementing a second competing state machine.

## Environment separation

Keep CrewAI and OpenHands in separate Python environments. This avoids dependency collisions and preserves the existing dedicated OpenHands environment.

CrewAI/orchestration environment:

```powershell
py -m venv .venv-crewai
.\.venv-crewai\Scripts\python -m pip install -r requirements-crewai.txt
```

OpenHands environment remains the existing `.venv-openhands`. You can override its interpreter path with `OPENHANDS_PYTHON`.

## Run directly

```powershell
.\.venv-crewai\Scripts\python run_integrated.py --repo "C:\path\to\repo" --task "Fix the requested bug" --orchestrator crewai
```

The normal result stops at `READY_TO_APPLY`. To apply from the local CLI only after explicit approval:

```powershell
python finalize_goal.py apply --goal-id GOAL-... --apply
```

`--auto-apply` exists for deliberate local automation, but the OpenClaw bridge intentionally never forwards it.

## Native fallback

If CrewAI is unavailable, the same thin engine can be exercised without CrewAI for diagnostics:

```powershell
python run_integrated.py --repo "C:\path\to\repo" --task "..." --orchestrator native
```

This fallback is useful for testing; the intended integrated path is `--orchestrator crewai`.
