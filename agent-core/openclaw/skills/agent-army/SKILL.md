---
name: agent-army
description: Run the local CrewAI-orchestrated coding pipeline with OpenHands while preserving agent-core safety gates.
user-invocable: true
---

# Agent Army

Use this skill when the user wants the local agent army to inspect, plan, review, or implement a coding task in a git repository.

The bridge is intentionally non-applying: it may create a reviewed patch, but it must never apply the patch to the real target repository. Final apply remains an explicit operator action.

## Required setup

The OpenClaw Gateway process must have `AGENT_ARMY_CORE_DIR` set to the absolute path of the project's `agent-core` directory and `AGENT_ARMY_CREWAI_PYTHON` set to the Python interpreter where `requirements-crewai.txt` is installed.

## Run a new task safely

1. Confirm the target repository path and the user's coding task from the conversation.
2. Use the file-writing tool to create a JSON request inside the OpenClaw workspace, for example `.agent-army/request.json`:

```json
{
  "repo": "C:/path/to/target/repo",
  "task": "Fix the calculator rounding bug"
}
```

3. Use `exec` only to run the fixed dispatcher command below. Do not interpolate the task text into a shell command:

```bash
python "{baseDir}/scripts/dispatch.py" --request-file ".agent-army/request.json"
```

4. Report the returned `goal_id`, final state, and next action to the user.
5. If the state is `READY_TO_APPLY`, tell the user that explicit apply approval is still required. Do not run an apply command automatically.

## Resume an existing goal

Write a request file containing only the goal id:

```json
{
  "goal_id": "GOAL-20260817-0001"
}
```

Then run the same dispatcher command.
