# OpenClaw integration

OpenClaw is the chat/gateway entry point. It does not replace the project's state machine and it does not modify the target repository directly.

Flow:

`OpenClaw message -> agent-army skill -> run_integrated.py -> CrewAI Flow -> existing Goal services -> OpenHands builder -> deterministic final review`

## Install the skill

From `agent-core`:

```powershell
openclaw skills install .\openclaw\skills\agent-army --as agent-army
```

Set `AGENT_ARMY_CORE_DIR` to the absolute `agent-core` path and `AGENT_ARMY_CREWAI_PYTHON` to `.venv-crewai\Scripts\python.exe` (or the equivalent interpreter on your OS) in the environment used to start the OpenClaw Gateway. Then restart the gateway and verify the skill:

```powershell
openclaw gateway restart
openclaw skills list
```

The dispatcher never forwards `--auto-apply`; messages arriving through OpenClaw can build and review a patch, but applying it to the real repository remains a separate explicit operator action.
