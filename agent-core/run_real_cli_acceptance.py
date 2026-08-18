from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _safe_rmtree(path: Path | str) -> None:
    path_obj = Path(path)
    if not path_obj.exists():
        return
    def _onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path_obj, onerror=_onerror)


def _setup_fixture_repo(repo_path: Path) -> None:
    if repo_path.exists():
        _safe_rmtree(repo_path)
    repo_path.mkdir(parents=True, exist_ok=True)

    calc_content = "def add(a, b):\n    return a + b\n"
    test_content = """import unittest
import inspect
import calculator


class CalculatorTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(calculator.add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
"""
    (repo_path / "calculator.py").write_text(calc_content, encoding="utf-8")
    (repo_path / "test_calculator.py").write_text(test_content, encoding="utf-8")
    (repo_path / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\nruntime/\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "AgentAcceptance"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "agent@acceptance.local"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial calculator fixture"], cwd=repo_path, capture_output=True, check=True)


def _run_cli(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    print(f"\n>> CLI: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(f"[stderr] {result.stderr.strip()}")
    return result


def _read_persisted_state(goal_dir: Path) -> dict:
    goal_json = goal_dir / "goal.json"
    if not goal_json.exists():
        raise AssertionError(f"Missing persisted goal.json at {goal_json}")
    return json.loads(goal_json.read_text(encoding="utf-8"))


def run_single_cli_cycle(cycle_index: int, fixture_path: Path, root_dir: Path, py_exe: str) -> dict:
    print(f"\n=======================================================")
    print(f"STARTING PRODUCTION CLI ACCEPTANCE CYCLE #{cycle_index}")
    print(f"=======================================================")

    goal_text = (
        "calculator.py içindeki add(a, b) fonksiyonuna Python type hint ekle. "
        "Fonksiyonun davranışını değiştirme. "
        "Sadece calculator.py dosyasını değiştir. "
        "add fonksiyonu def add(a: int, b: int) -> int: biçiminde olmalı. "
        "Mevcut testler aynı şekilde geçmeye devam etmeli. "
        "Yeni dependency ekleme, refactor yapma ve başka dosyalara dokunma."
    )

    # 1. INTAKE (CREATED) via real goal_cli.py
    intake_res = _run_cli([py_exe, "goal_cli.py", "--repo", str(fixture_path), "--goal", goal_text], cwd=root_dir)
    if intake_res.returncode != 0:
        raise AssertionError(f"goal_cli.py failed with exit code {intake_res.returncode}")
    
    # Parse real ID from stdout
    match = re.search(r"ID:\s*(GOAL-\d{8}-\d{4})", intake_res.stdout)
    if not match:
        raise AssertionError(f"Could not parse Goal ID from goal_cli stdout:\n{intake_res.stdout}")
    goal_id = match.group(1)
    print(f"[ASSERT] Real Parsed Goal ID: {goal_id}")

    goal_dir = root_dir / "runtime" / "goals" / goal_id
    state = _read_persisted_state(goal_dir)
    assert state["status"] == "CREATED", f"Expected CREATED, got {state['status']}"
    print(f"[OK] State after intake: {state['status']}")

    # 2. PLANNING (PLANNED) via real run_goal.py
    plan_res = _run_cli([py_exe, "run_goal.py", "--goal-id", goal_id], cwd=root_dir)
    if plan_res.returncode != 0:
        raise AssertionError(f"run_goal.py failed with exit code {plan_res.returncode}")
    
    state = _read_persisted_state(goal_dir)
    assert state["status"] == "PLANNED", f"Expected PLANNED, got {state['status']}"
    plan_file = goal_dir / "plan.json"
    assert plan_file.exists(), f"Missing {plan_file}"
    plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
    assert "calculator.py" in plan_data.get("allowed_files", []), "calculator.py must be in allowed_files"
    print(f"[OK] State after planning: {state['status']}")

    # 3. REVIEW (APPROVED) via real review_goal.py (NO FORCED OVERRIDES!)
    rev_res = _run_cli([py_exe, "review_goal.py", "--goal-id", goal_id], cwd=root_dir)
    if rev_res.returncode != 0:
        raise AssertionError(f"review_goal.py failed with exit code {rev_res.returncode}")
    
    state = _read_persisted_state(goal_dir)
    assert state["status"] == "APPROVED", f"Expected APPROVED naturally from reviewer quorum, got {state['status']}"
    review_file = goal_dir / "review.json"
    assert review_file.exists(), f"Missing {review_file}"
    review_data = json.loads(review_file.read_text(encoding="utf-8"))
    assert review_data.get("final_verdict") == "PASS", f"Expected review final_verdict PASS, got {review_data.get('final_verdict')}"
    print(f"[OK] State after review: {state['status']} (Verdict: {review_data.get('final_verdict')})")

    # 4. COMPLEXITY ASSESSMENT (READY_FOR_OPENHANDS) via real assess_goal.py
    comp_res = _run_cli([py_exe, "assess_goal.py", "--goal-id", goal_id, "--force"], cwd=root_dir)
    if comp_res.returncode != 0:
        raise AssertionError(f"assess_goal.py failed with exit code {comp_res.returncode}")
    
    state = _read_persisted_state(goal_dir)
    assert state["status"] == "READY_FOR_OPENHANDS", f"Expected READY_FOR_OPENHANDS, got {state['status']}"
    comp_file = goal_dir / "complexity.json"
    assert comp_file.exists(), f"Missing {comp_file}"
    print(f"[OK] State after complexity: {state['status']}")

    # Target invariant check before builder
    target_calc = (fixture_path / "calculator.py").read_text(encoding="utf-8")
    assert "def add(a, b):" in target_calc, "Target calculator.py must remain untouched before builder"

    # 5. BUILDER (BUILT_PENDING_REVIEW) via real run_builder.py
    build_res = _run_cli([py_exe, "run_builder.py", "--goal-id", goal_id, "--execute", "--mode", "relaxed-acceptance"], cwd=root_dir)
    if build_res.returncode != 0:
        raise AssertionError(f"run_builder.py failed with exit code {build_res.returncode}")
    
    state = _read_persisted_state(goal_dir)
    assert state["status"] == "BUILT_PENDING_REVIEW", f"Expected BUILT_PENDING_REVIEW, got {state['status']}"
    build_file = goal_dir / "build.json"
    assert build_file.exists(), f"Missing {build_file}"
    build_data = json.loads(build_file.read_text(encoding="utf-8"))
    assert build_data.get("openhands_executed") is True, f"Expected openhands_executed == True, got {build_data.get('openhands_executed')}"
    
    patch_file = goal_dir / "build.patch"
    assert patch_file.exists(), f"Missing {patch_file}"
    patch_text = patch_file.read_text(encoding="utf-8")
    assert len(patch_text.strip()) > 0, "build.patch must be non-empty"
    assert "def add(a: int, b: int) -> int:" in patch_text, "build.patch must contain type hint diff"
    print(f"[OK] State after builder: {state['status']} (Patch size: {len(patch_text)} bytes, OpenHands executed: True)")

    # Target invariant check after builder (must STILL be untouched!)
    target_calc = (fixture_path / "calculator.py").read_text(encoding="utf-8")
    assert "def add(a, b):" in target_calc, "Target calculator.py must remain untouched after builder before explicit apply"

    # 6. FINAL REVIEW (READY_TO_APPLY) via real finalize_goal.py review
    fin_rev_res = _run_cli([py_exe, "finalize_goal.py", "review", "--goal-id", goal_id], cwd=root_dir)
    if fin_rev_res.returncode != 0:
        raise AssertionError(f"finalize_goal.py review failed with exit code {fin_rev_res.returncode}")
    
    state = _read_persisted_state(goal_dir)
    assert state["status"] == "READY_TO_APPLY", f"Expected READY_TO_APPLY, got {state['status']}"
    final_rev_file = goal_dir / "final_review.json"
    assert final_rev_file.exists(), f"Missing {final_rev_file}"
    final_rev_data = json.loads(final_rev_file.read_text(encoding="utf-8"))
    assert final_rev_data.get("decision") == "PASS", f"Expected decision == PASS, got {final_rev_data.get('decision')}"
    assert final_rev_data.get("ready_to_apply") is True, "Expected ready_to_apply == True"
    print(f"[OK] State after final review: {state['status']}")

    # Target invariant check after final review (must STILL be untouched!)
    target_calc = (fixture_path / "calculator.py").read_text(encoding="utf-8")
    assert "def add(a, b):" in target_calc, "Target calculator.py must remain untouched after final review"

    # 7. EXPLICIT APPLY (COMPLETED) via real finalize_goal.py apply --apply
    apply_res = _run_cli([py_exe, "finalize_goal.py", "apply", "--goal-id", goal_id, "--apply"], cwd=root_dir)
    if apply_res.returncode != 0:
        raise AssertionError(f"finalize_goal.py apply failed with exit code {apply_res.returncode}")
    
    state = _read_persisted_state(goal_dir)
    assert state["status"] == "COMPLETED", f"Expected COMPLETED, got {state['status']}"
    apply_manifest = goal_dir / "apply_manifest.json"
    assert apply_manifest.exists(), f"Missing {apply_manifest}"
    final_verif = goal_dir / "final_verification.json"
    assert final_verif.exists(), f"Missing {final_verif}"
    print(f"[OK] State after explicit apply: {state['status']}")

    # 8. STATUS SNAPSHOT via real goal_ctl.py status
    ctl_res = _run_cli([py_exe, "goal_ctl.py", "status", "--goal-id", goal_id], cwd=root_dir)
    if ctl_res.returncode != 0:
        raise AssertionError(f"goal_ctl.py status failed with exit code {ctl_res.returncode}")
    assert f"ID: {goal_id}" in ctl_res.stdout, "goal_ctl status must output correct ID"
    assert "State: COMPLETED" in ctl_res.stdout, "goal_ctl status must show State: COMPLETED"
    print(f"[OK] goal_ctl status confirmed: COMPLETED")

    # 9. TARGET REPOSITORY VERIFICATION
    target_calc_final = (fixture_path / "calculator.py").read_text(encoding="utf-8")
    assert "def add(a: int, b: int) -> int:" in target_calc_final, "Target calculator.py must now have type hints applied"
    print(f"[OK] Target calculator.py successfully verified with type hints")

    test_run = subprocess.run([py_exe, "-m", "unittest", "test_calculator.py"], cwd=fixture_path, capture_output=True, text=True)
    assert test_run.returncode == 0, f"Target test_calculator.py failed:\n{test_run.stderr}"
    print(f"[OK] Target test_calculator.py passed")

    return {
        "cycle": cycle_index,
        "goal_id": goal_id,
        "final_state": state["status"],
        "openhands_executed": build_data.get("openhands_executed"),
        "patch_size": len(patch_text),
        "final_review_decision": final_rev_data.get("decision"),
        "final_review_ready": final_rev_data.get("ready_to_apply"),
        "apply_manifest_exists": apply_manifest.exists(),
        "ctl_status": "COMPLETED",
    }


def main() -> int:
    root_dir = Path(__file__).resolve().parent
    fixture_repo = root_dir / "temp" / "openhands-live-fixture"
    py_exe = str(root_dir / ".venv-openhands" / "Scripts" / "python.exe")
    if not Path(py_exe).exists():
        py_exe = sys.executable

    print("=================================================================")
    print("REAL PRODUCTION CLI ACCEPTANCE MATRIX")
    print(f"Root: {root_dir}")
    print(f"Fixture: {fixture_repo}")
    print(f"Python: {py_exe}")
    print("=================================================================")

    results = []
    for cycle in (1, 2):
        if cycle > 1:
            print("\n[Cooldown] Waiting 15s between real CLI acceptance cycles...")
            time.sleep(15)
        _setup_fixture_repo(fixture_repo)
        res = run_single_cli_cycle(cycle, fixture_repo, root_dir, py_exe)
        results.append(res)

    print("\n=================================================================")
    print("ALL TWO PRODUCTION CLI ACCEPTANCE CYCLES PASSED!")
    print("=================================================================")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
