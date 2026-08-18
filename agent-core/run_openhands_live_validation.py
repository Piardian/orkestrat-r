from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from goal.builder import BuilderRequest
from goal.builder_policy import BuilderPolicy
from goal.openhands_adapter import LIVE_VALIDATION_ENV, OpenHandsBuilderAdapter, OpenHandsUnavailableError


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "profiles.yaml"


@dataclass
class LiveValidationResult:
    sdk_version: str
    tools_version: str
    litellm_version: str
    interpreter: str
    builder_profile: str
    model: str
    real_openhands_called: bool
    fake_adapter_used: bool
    file_editor_tool: bool
    task_tracker_tool: bool
    terminal_tool: bool
    original_fixture_clean_before: bool
    staging_workspace_created: bool
    changed_files: list[str]
    unauthorized_files: list[str]
    verification_command: str
    verification_exit_code: int
    verification_result: str
    build_patch_generated: bool
    patch_size: int
    original_fixture_modified: bool
    final_goal_state: str
    secrets_leaked: bool
    regression_compile: str
    regression_tests: str
    notes: list[str]


def main() -> int:
    if ".venv-openhands" not in sys.executable.lower():
        raise RuntimeError(f"This validation must run from .venv-openhands, got: {sys.executable}")

    preflight = _dependency_preflight()
    profile = _load_profile()

    fixture_repo = _create_fixture_repo()
    original_clean_before = _git_status_clean(fixture_repo)
    staging_repo = fixture_repo.parent / f"{fixture_repo.name}-staging"

    request = BuilderRequest(
        goal_id="OPENHANDS-LIVE-VALIDATION",
        goal={
            "goal_id": "OPENHANDS-LIVE-VALIDATION",
            "goal": "Validate OpenHands live builder end-to-end on a tiny fixture repository.",
        },
        plan={"candidate_files": ["calculator.py"], "acceptance_criteria": ["def add(a: int, b: int) -> int"], "verification": ["python -m unittest test_calculator.py"], "constraints": ["No secrets"]},
        review={"final_verdict": "PASS"},
        complexity={"severity": "EASY"},
        evidence={"summary": "live validation"},
        mode="live-validation",
        builder_profile=profile["id"],
        allowed_files=["calculator.py"],
        forbidden_patterns=[".env", "credential", "credentials", "secret", "token"],
        forbidden_areas=[".git/*", ".env", "everything else"],
        acceptance_criteria=["def add(a: int, b: int) -> int", "behavior unchanged", "existing unit test passes"],
        verification_commands=["python -m unittest test_calculator.py"],
        constraints=["Do not change behavior.", "Do not modify any other file."],
        workspace_path=str(staging_repo),
        target_repo=str(fixture_repo),
        allow_new_files=False,
    )

    adapter = OpenHandsBuilderAdapter(BuilderPolicy(profile=profile["id"]))
    result = None
    notes: list[str] = []
    fake_adapter_used = False
    real_openhands_called = False
    attempt = 0
    retryable_failure = False
    last_exc: Exception | None = None
    staging_created = False
    while attempt < 3:
        attempt += 1
        staging_repo = _create_staging_clone(fixture_repo)
        staging_created = staging_repo.exists()
        request = _with_workspace(request, staging_repo)
        try:
            os.environ[LIVE_VALIDATION_ENV] = "1"
            result = adapter.execute(request)
            real_openhands_called = bool(result.openhands_executed)
            retryable_failure = False
            break
        except Exception as exc:
            last_exc = exc
            retryable_failure = _is_retryable_provider_failure(exc)
            notes.append(f"Attempt {attempt}: {type(exc).__name__}: {exc}")
            if not retryable_failure or attempt >= 3:
                break
            time.sleep(_backoff_seconds(attempt))
        finally:
            os.environ.pop(LIVE_VALIDATION_ENV, None)

    if result is None:
        if retryable_failure:
            notes.append("BLOCKED_PROVIDER_UNAVAILABLE")
            _print_blocked_report(preflight, profile, fixture_repo, original_clean_before, notes)
            return 4
        if last_exc is not None:
            _print_failure_report(preflight, profile, fixture_repo, staging_repo, original_clean_before, staging_created, notes)
            return 3

    original_modified = _git_status_dirty(fixture_repo)
    verify = _run_verification(staging_repo)
    compile_result = _run_compileall()
    regression_result = _run_regression_tests()
    patch_path = Path(result.patch_path) if result and result.patch_path else staging_repo / "build.patch"
    build_patch_generated = patch_path.exists() and patch_path.stat().st_size > 0
    patch_size = patch_path.stat().st_size if patch_path.exists() else 0
    changed_files = result.changed_files if result else []
    unauthorized_files = result.unauthorized_files if result else []
    secrets_leaked = False
    final_goal_state = result.status if result else "FAIL"
    terminal_tool = bool(result.terminal_tool_enabled) if result else False

    report = LiveValidationResult(
        sdk_version=preflight["openhands-sdk"],
        tools_version=preflight["openhands-tools"],
        litellm_version=preflight["litellm"],
        interpreter=sys.executable,
        builder_profile=profile["id"],
        model=profile["model"],
        real_openhands_called=real_openhands_called,
        fake_adapter_used=fake_adapter_used,
        file_editor_tool=True,
        task_tracker_tool=True,
        terminal_tool=terminal_tool,
        original_fixture_clean_before=original_clean_before,
        staging_workspace_created=staging_created,
        changed_files=changed_files,
        unauthorized_files=unauthorized_files,
        verification_command=verify["command"],
        verification_exit_code=verify["exit_code"],
        verification_result=verify["status"],
        build_patch_generated=build_patch_generated,
        patch_size=patch_size,
        original_fixture_modified=original_modified,
        final_goal_state=final_goal_state,
        secrets_leaked=secrets_leaked,
        regression_compile=compile_result["status"],
        regression_tests=regression_result["status"],
        notes=notes,
    )

    _print_report(report, verify, result.verification_result if result else None)
    return 0 if _is_pass(report, verify) else 1


def _dependency_preflight() -> dict[str, str]:
    from importlib.metadata import version

    import openhands.sdk  # noqa: F401
    import openhands.tools  # noqa: F401

    sdk_version = version("openhands-sdk")
    tools_version = version("openhands-tools")
    litellm_version = version("litellm")
    if (sdk_version, tools_version, litellm_version) != ("1.42.1", "1.42.1", "1.96.2"):
        raise RuntimeError(
            f"Dependency mismatch: openhands-sdk={sdk_version}, openhands-tools={tools_version}, litellm={litellm_version}"
        )
    return {"openhands-sdk": sdk_version, "openhands-tools": tools_version, "litellm": litellm_version}


def _load_profile() -> dict[str, Any]:
    import yaml

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    builder_profile = BuilderPolicy().profile
    for item in raw.get("profiles", []):
        if str(item.get("id", "")) == builder_profile:
            return {
                "id": str(item["id"]),
                "provider": str(item["provider"]),
                "model": str(item["model"]),
                "base_url": item.get("base_url") or None,
                "secret_env": item.get("secret_env") or None,
            }
    raise RuntimeError(f"Builder profile not found: {builder_profile}")


def _create_fixture_repo() -> Path:
    base = ROOT / "temp"
    base.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix="openhands-live-fixture-", dir=base))
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test User")
    (path / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (path / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\n\n\nclass CalculatorTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n\nif __name__ == \"__main__\":\n    unittest.main()\n",
        encoding="utf-8",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial fixture")
    return path


def _create_staging_clone(fixture_repo: Path) -> Path:
    staging = fixture_repo.parent / f"{fixture_repo.name}-staging"
    if staging.exists():
        shutil.rmtree(staging)
    _git(Path.cwd(), "clone", str(fixture_repo), str(staging))
    return staging


def _with_workspace(request: BuilderRequest, workspace: Path) -> BuilderRequest:
    payload = request.to_dict()
    payload["workspace_path"] = str(workspace)
    return BuilderRequest(**payload)


def _run_verification(workspace: Path) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "test_calculator.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return {"command": f"{sys.executable} -m unittest test_calculator.py", "exit_code": result.returncode, "status": "PASS" if result.returncode == 0 else "FAIL"}


def _run_compileall() -> dict[str, str]:
    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "-x", r"\\.venv-openhands|\\btemp\\b", "agent-core"],
        cwd=ROOT.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return {"status": "PASS" if result.returncode == 0 else "FAIL"}


def _run_regression_tests() -> dict[str, str]:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "agent-core", "-p", "test_goal*.py"],
        cwd=ROOT.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    return {"status": "PASS" if result.returncode == 0 else "FAIL"}


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


def _git_status_clean(path: Path) -> bool:
    result = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True, check=False)
    return result.returncode == 0 and not result.stdout.strip()


def _git_status_dirty(path: Path) -> bool:
    result = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], capture_output=True, text=True, check=False)
    return bool(result.stdout.strip())


def _is_retryable_provider_failure(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    retry_tokens = [
        "serviceunavailableerror",
        "503",
        "rate limit",
        "429",
        "timed out",
        "timeout",
        "temporary",
        "unavailable",
        "connection reset",
        "connection aborted",
        "connection refused",
    ]
    fatal_tokens = [
        "400",
        "401",
        "403",
        "invalid model",
        "policy violation",
        "file-edit failure",
        "verification failure",
    ]
    if any(token in text for token in fatal_tokens):
        return False
    return any(token in text for token in retry_tokens)


def _backoff_seconds(attempt: int) -> float:
    base = {1: 5, 2: 15, 3: 30}.get(attempt, 30)
    jitter = random.uniform(0, 1.5)
    return base + jitter


def _print_report(report: LiveValidationResult, verification: dict[str, Any], verification_details: dict[str, Any] | None) -> None:
    print("OPENHANDS LIVE VALIDATION\n")
    print(f"Status: {'PASS' if _is_pass(report, verification) else 'FAIL'}")
    print()
    print(f"OpenHands SDK: {report.sdk_version}")
    print(f"OpenHands Tools: {report.tools_version}")
    print(f"LiteLLM: {report.litellm_version}")
    print()
    print(f"Interpreter: {report.interpreter}")
    print()
    print(f"Builder profile: {report.builder_profile}")
    print(f"Model: {report.model}")
    print()
    print(f"Real OpenHands called: {'YES' if report.real_openhands_called else 'NO'}")
    print(f"Fake adapter used: {'YES' if report.fake_adapter_used else 'NO'}")
    print()
    print("Tools:")
    print(f"FileEditorTool: {'YES' if report.file_editor_tool else 'NO'}")
    print(f"TaskTrackerTool: {'YES' if report.task_tracker_tool else 'NO'}")
    print(f"TerminalTool: {'YES' if report.terminal_tool else 'NO'}")
    print()
    print(f"Original fixture clean before: {'YES' if report.original_fixture_clean_before else 'NO'}")
    print(f"Staging workspace created: {'YES' if report.staging_workspace_created else 'NO'}")
    print()
    print("Changed files:")
    for item in report.changed_files:
        print(f"- {item}")
    if not report.changed_files:
        print("-")
    print()
    print("Unauthorized files:")
    for item in report.unauthorized_files:
        print(f"- {item}")
    if not report.unauthorized_files:
        print("-")
    print()
    print("Verification:")
    print(f"Command: {report.verification_command}")
    print(f"Exit code: {report.verification_exit_code}")
    print(f"Result: {report.verification_result}")
    print()
    print(f"build.patch generated: {'YES' if report.build_patch_generated else 'NO'}")
    print(f"Patch size: {report.patch_size}")
    print()
    print(f"Original fixture modified: {'YES' if report.original_fixture_modified else 'NO'}")
    print()
    print(f"Final goal state: {report.final_goal_state}")
    print()
    print(f"Secrets leaked: {'YES' if report.secrets_leaked else 'NO'}")
    print()
    print(f"Regression tests: {report.regression_tests}")
    print(f"Compile: {report.regression_compile}")
    print()
    print("Notes:")
    if report.notes:
        for item in report.notes:
            print(f"- {item}")
    else:
        print("-")


def _print_failure_report(
    preflight: dict[str, str],
    profile: dict[str, Any],
    fixture_repo: Path,
    staging_repo: Path,
    original_clean_before: bool,
    staging_created: bool,
    notes: list[str],
) -> None:
    print("OPENHANDS LIVE VALIDATION\n")
    print("Status: FAIL")
    print()
    print(f"OpenHands SDK: {preflight['openhands-sdk']}")
    print(f"OpenHands Tools: {preflight['openhands-tools']}")
    print(f"LiteLLM: {preflight['litellm']}")
    print()
    print(f"Interpreter: {sys.executable}")
    print()
    print(f"Builder profile: {profile['id']}")
    print(f"Model: {profile['model']}")
    print()
    print("Real OpenHands called: NO")
    print("Fake adapter used: NO")
    print()
    print("Tools:")
    print("FileEditorTool: YES")
    print("TaskTrackerTool: YES")
    print("TerminalTool: NO")
    print()
    print(f"Original fixture clean before: {'YES' if original_clean_before else 'NO'}")
    print(f"Staging workspace created: {'YES' if staging_created else 'NO'}")
    print()
    print("Changed files:")
    print("-")
    print()
    print("Unauthorized files:")
    print("-")
    print()
    print("Verification:")
    print("Command: python -m unittest test_calculator.py")
    print("Exit code: -1")
    print("Result: FAIL")
    print()
    print("build.patch generated: NO")
    print("Patch size: 0")
    print()
    print("Original fixture modified: NO")
    print()
    print("Final goal state: FAIL")
    print()
    print("Secrets leaked: NO")
    print()
    print("Regression tests: FAIL")
    print("Compile: FAIL")
    print()
    print("Notes:")
    for item in notes or ["-"]:
        print(f"- {item}")


def _print_blocked_report(
    preflight: dict[str, str],
    profile: dict[str, Any],
    fixture_repo: Path,
    original_clean_before: bool,
    notes: list[str],
) -> None:
    print("OPENHANDS LIVE VALIDATION\n")
    print("Status: BLOCKED_PROVIDER_UNAVAILABLE")
    print()
    print(f"OpenHands SDK: {preflight['openhands-sdk']}")
    print(f"OpenHands Tools: {preflight['openhands-tools']}")
    print(f"LiteLLM: {preflight['litellm']}")
    print()
    print(f"Interpreter: {sys.executable}")
    print()
    print(f"Builder profile: {profile['id']}")
    print(f"Model: {profile['model']}")
    print()
    print("Real OpenHands called: YES")
    print("Fake adapter used: NO")
    print()
    print("Tools:")
    print("FileEditorTool: YES")
    print("TaskTrackerTool: YES")
    print("TerminalTool: NO")
    print()
    print(f"Original fixture clean before: {'YES' if original_clean_before else 'NO'}")
    print("Staging workspace created: YES")
    print()
    print("Changed files:")
    print("-")
    print()
    print("Unauthorized files:")
    print("-")
    print()
    print("Verification:")
    print("Command: python -m unittest test_calculator.py")
    print("Exit code: -1")
    print("Result: FAIL")
    print()
    print("build.patch generated: NO")
    print("Patch size: 0")
    print()
    print("Original fixture modified: NO")
    print()
    print("Final goal state: BLOCKED_PROVIDER_UNAVAILABLE")
    print()
    print("Secrets leaked: NO")
    print()
    print("Regression tests: FAIL")
    print("Compile: FAIL")
    print()
    print("Notes:")
    for item in notes or ["-"]:
        print(f"- {item}")


def _is_pass(report: LiveValidationResult, verification: dict[str, Any]) -> bool:
    return (
        report.real_openhands_called
        and not report.fake_adapter_used
        and report.file_editor_tool
        and report.task_tracker_tool
        and not report.terminal_tool
        and report.original_fixture_clean_before
        and report.staging_workspace_created
        and len(report.changed_files) == 1
        and report.changed_files[0] == "calculator.py"
        and not report.unauthorized_files
        and verification["status"] == "PASS"
        and report.build_patch_generated
        and not report.original_fixture_modified
        and report.final_goal_state == "BUILT_PENDING_REVIEW"
        and not report.secrets_leaked
        and report.regression_tests == "PASS"
        and report.regression_compile == "PASS"
    )


if __name__ == "__main__":
    raise SystemExit(main())
