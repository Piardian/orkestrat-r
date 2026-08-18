from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from goal.verification_command import (
    normalize_verification_command,
    is_safe_verification_command,
    run_single_verification,
    run_verification_suite,
)


class VerificationCommandTests(unittest.TestCase):
    def test_windows_executable_path_with_spaces_is_normalized(self) -> None:
        command = r"C:\some folder\venv\Scripts\python.exe test_calculator.py"
        self.assertEqual(
            normalize_verification_command(command),
            [sys.executable, "test_calculator.py"],
        )

    def test_structured_argv_passthrough_with_python_resolution(self) -> None:
        argv = ["python", "test_calculator.py"]
        self.assertEqual(normalize_verification_command(argv), [sys.executable, "test_calculator.py"])

    def test_python_commands_resolve_to_sys_executable(self) -> None:
        for py_cmd in ("python", "python.exe", "python3", "python3.exe", "py", "py.exe"):
            self.assertEqual(
                normalize_verification_command(f"{py_cmd} test.py"),
                [sys.executable, "test.py"],
            )

    def test_direct_python_script_resolves_with_interpreter(self) -> None:
        self.assertEqual(
            normalize_verification_command("test_calculator.py"),
            [sys.executable, "test_calculator.py"],
        )

    def test_unittest_command_normalized(self) -> None:
        self.assertEqual(
            normalize_verification_command("unittest test_calc.py"),
            [sys.executable, "-m", "unittest", "test_calc.py"],
        )

    def test_safe_command_check(self) -> None:
        self.assertTrue(is_safe_verification_command([sys.executable, "test.py"]))
        self.assertFalse(is_safe_verification_command(["rm", "-rf", "/"]))
        self.assertFalse(is_safe_verification_command(["del", "file.txt"]))
        self.assertFalse(is_safe_verification_command(["git", "push", "origin", "main"]))
        self.assertFalse(is_safe_verification_command(["git", "clean", "-f"]))

    def test_run_single_verification_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            script = cwd / "test_ok.py"
            script.write_text("print('PASS'); import sys; sys.exit(0)", encoding="utf-8")
            res = run_single_verification(f"python {script.name}", cwd=cwd)
            self.assertEqual(res["status"], "PASS")
            self.assertEqual(res["exit_code"], 0)
            self.assertIn("PASS", res["stdout"])
            self.assertIsNone(res["failure_code"])

    def test_run_single_verification_nonzero_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            script = cwd / "test_fail.py"
            script.write_text("import sys; sys.exit(42)", encoding="utf-8")
            res = run_single_verification(f"python {script.name}", cwd=cwd)
            self.assertEqual(res["status"], "FAIL")
            self.assertEqual(res["exit_code"], 42)
            self.assertEqual(res["failure_code"], "NONZERO_EXIT")

    def test_run_single_verification_executable_not_found(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            res = run_single_verification("nonexistent_binary_xyz123 --arg", cwd=cwd)
            self.assertEqual(res["status"], "FAIL")
            self.assertEqual(res["exit_code"], 127)
            self.assertEqual(res["failure_code"], "EXECUTABLE_NOT_FOUND")

    def test_run_single_verification_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            script = cwd / "test_hang.py"
            script.write_text("import time; time.sleep(10)", encoding="utf-8")
            res = run_single_verification(f"python {script.name}", cwd=cwd, timeout=0.5)
            self.assertEqual(res["status"], "FAIL")
            self.assertEqual(res["exit_code"], 124)
            self.assertEqual(res["failure_code"], "VERIFICATION_TIMEOUT")

    def test_run_single_verification_cwd_missing(self) -> None:
        res = run_single_verification("python test.py", cwd=Path("C:/nonexistent_dir_123456789"))
        self.assertEqual(res["status"], "FAIL")
        self.assertEqual(res["exit_code"], 126)
        self.assertEqual(res["failure_code"], "CWD_NOT_FOUND")

    def test_run_single_verification_unsafe_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            res = run_single_verification("rm -rf /", cwd=cwd)
            self.assertEqual(res["status"], "FAIL")
            self.assertEqual(res["failure_code"], "UNSAFE_COMMAND")

    def test_run_verification_suite_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            script1 = cwd / "test1.py"
            script1.write_text("print('t1')", encoding="utf-8")
            script2 = cwd / "test2.py"
            script2.write_text("print('t2')", encoding="utf-8")
            res = run_verification_suite([f"python {script1.name}", f"python {script2.name}"], cwd=cwd)
            self.assertEqual(res["status"], "PASS")
            self.assertEqual(len(res["command_results"]), 2)

    def test_run_verification_suite_stops_on_first_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            script1 = cwd / "test_fail.py"
            script1.write_text("import sys; sys.exit(1)", encoding="utf-8")
            script2 = cwd / "test_pass.py"
            script2.write_text("print('pass')", encoding="utf-8")
            res = run_verification_suite([f"python {script1.name}", f"python {script2.name}"], cwd=cwd)
            self.assertEqual(res["status"], "FAIL")
    def test_grep_fallback_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            target = cwd / "sample.txt"
            target.write_text("hello world target", encoding="utf-8")
            res_pass = run_single_verification("grep 'world' sample.txt", cwd=cwd)
            self.assertEqual(res_pass["status"], "PASS")
            self.assertEqual(res_pass["exit_code"], 0)

            res_fail = run_single_verification("grep 'nonexistent' sample.txt", cwd=cwd)
            self.assertEqual(res_fail["status"], "FAIL")
            self.assertEqual(res_fail["exit_code"], 1)


if __name__ == "__main__":
    unittest.main()
