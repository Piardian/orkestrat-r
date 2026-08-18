from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .finalize import FinalReviewService as BaseFinalReviewService
from .verification_sandbox import run_docker_verification_suite


class SandboxedFinalReviewService(BaseFinalReviewService):
    """Final review that can execute verification outside the host environment."""

    def _run_verification(self, workspace: Path, commands: list[str]) -> dict[str, Any]:
        mode = os.getenv("AGENT_ARMY_VERIFICATION_SANDBOX", "host").strip().lower()
        if mode == "docker":
            return run_docker_verification_suite(
                commands,
                workspace,
                timeout=float(os.getenv("AGENT_ARMY_FINAL_VERIFY_TIMEOUT_SECONDS", "300")),
            )
        if mode not in {"host", "local"}:
            raise ValueError(f"Unsupported AGENT_ARMY_VERIFICATION_SANDBOX: {mode}")
        return super()._run_verification(workspace, commands)
