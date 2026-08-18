from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
from typing import Any

_WINDOWS_EXECUTABLE_RE = re.compile(r"^(?P<exe>.+?\.(?:exe|cmd|bat|ps1))\s+(?P<args>.+)$", re.IGNORECASE)
MAX_VERIFICATION_OUTPUT_BYTES = 8192


def _clip_output(text: str) -> str:
    encoded = (text or "").encode("utf-8", errors="replace")
    if len(encoded) <= MAX_VERIFICATION_OUTPUT_BYTES:
        return text or ""
    return encoded[:MAX_VERIFICATION_OUTPUT_BYTES].decode("utf-8", errors="ignore")


def _is_pytest_available() -> bool:
    if shutil.which("pytest"):
        return True
    try:
        return importlib.util.find_spec("pytest") is not None
    except Exception:
        return False


def normalize_verification_command(command: Any, cwd: Path | str | None = None) -> list[str]:
    if isinstance(command, (list, tuple)):
        raw_argv = [str(item).strip() for item in command if str(item).strip()]
    else:
        text = str(command or "").strip()
        if not text:
            return []
        match = _WINDOWS_EXECUTABLE_RE.match(text)
        if match:
            exe = match.group("exe")
            raw_args = shlex.split(match.group("args"), posix=True)
            raw_argv = [exe, *raw_args]
        else:
            raw_argv = shlex.split(text, posix=True)

    if not raw_argv:
        return []

    exe_raw = raw_argv[0]
    args = raw_argv[1:]
    exe_name = Path(exe_raw).name.lower()

    # 1. Environment-aware Python interpreter normalization
    if exe_name in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
        cleaned_args = list(args)
        if exe_name in {"py", "py.exe"}:
            while cleaned_args and (re.match(r"^-[23](\.\d+)?(-64|-32)?$", cleaned_args[0]) or cleaned_args[0].startswith("-V:")):
                cleaned_args.pop(0)
        return [sys.executable, *cleaned_args]

    # 2. Pytest normalization
    if exe_name in {"pytest", "pytest.exe"}:
        if shutil.which(exe_raw):
            return [exe_raw, *args]
        if _is_pytest_available():
            return [sys.executable, "-m", "pytest", *args]
        # Fallback if pytest is not installed: run via unittest or direct script
        if args and args[0].endswith(".py"):
            return [sys.executable, "-m", "unittest", *args]
        return [sys.executable, "-m", "unittest"]

    # 3. Unittest normalization
    if exe_name in {"unittest", "unittest.exe"}:
        return [sys.executable, "-m", "unittest", *args]

    # 4. Direct Python script execution in cwd (e.g. "test_calculator.py" or "./test_calculator.py")
    if exe_name.endswith(".py"):
        return [sys.executable, exe_raw, *args]

    # 5. Deterministic Python fallback for shell utilities (grep, cat, echo)
    if exe_name in {"grep", "egrep", "fgrep"}:
        opts = [a for a in args if a.startswith("-") and len(a) > 1]
        positional = [a for a in args if not (a.startswith("-") and len(a) > 1)]
        if positional:
            pattern = positional[0]
            files = positional[1:]
            flags = "re.IGNORECASE" if any("i" in o for o in opts) else "0"
            invert = any("v" in o for o in opts)
            py_code = (
                "import sys, re, pathlib\n"
                f"pat = {repr(pattern)}\n"
                f"files = {repr(files)} or ['.']\n"
                "found = False\n"
                "for f in files:\n"
                "    p = pathlib.Path(f)\n"
                "    if p.is_file():\n"
                "        txt = p.read_text(encoding='utf-8', errors='replace')\n"
                f"        if bool(re.search(pat, txt, {flags})) != {repr(invert)}:\n"
                "            found = True\n"
                "            break\n"
                "sys.exit(0 if found else 1)\n"
            )
            return [sys.executable, "-c", py_code]

    if exe_name == "cat" and not shutil.which(exe_raw):
        py_code = "import sys, pathlib; [sys.stdout.write(pathlib.Path(f).read_text(encoding='utf-8', errors='replace')) for f in sys.argv[1:] if pathlib.Path(f).is_file()]"
        return [sys.executable, "-c", py_code, *args]

    # 6. Resolve relative path to executable if in cwd
    if cwd is not None and not Path(exe_raw).is_absolute():
        cwd_path = Path(cwd)
        local_target = cwd_path / exe_raw
        if local_target.exists() and local_target.is_file():
            if local_target.suffix.lower() == ".py":
                return [sys.executable, str(local_target), *args]

    return raw_argv


def is_safe_verification_command(argv: list[str]) -> bool:
    if not argv:
        return False
    exe = Path(argv[0]).name.lower()
    destructive_exes = {"rm", "del", "rmdir", "format", "shutdown"}
    if exe in destructive_exes:
        return False
    if exe == "git" and len(argv) > 1:
        git_sub = argv[1].lower()
        if git_sub in {"push", "clean"}:
            return False
        if git_sub == "reset" and "--hard" in [a.lower() for a in argv]:
            return False
    return True


def run_single_verification(
    command: Any,
    cwd: Path | str,
    timeout: float = 30.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    cwd_path = Path(cwd)
    if not cwd_path.exists() or not cwd_path.is_dir():
        return {
            "command": str(command),
            "argv": [],
            "status": "FAIL",
            "exit_code": 126,
            "stdout": "",
            "stderr": f"Verification working directory not found: {cwd}",
            "duration_ms": 0,
            "failure_code": "CWD_NOT_FOUND",
        }

    argv = normalize_verification_command(command, cwd=cwd_path)
    if not argv:
        return {
            "command": str(command),
            "argv": [],
            "status": "FAIL",
            "exit_code": 127,
            "stdout": "",
            "stderr": "Empty or invalid verification command",
            "duration_ms": 0,
            "failure_code": "INVALID_COMMAND",
        }

    if not is_safe_verification_command(argv):
        return {
            "command": str(command),
            "argv": argv,
            "status": "FAIL",
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Unsafe verification command rejected: {command}",
            "duration_ms": 0,
            "failure_code": "UNSAFE_COMMAND",
        }

    run_env = dict(os.environ)
    run_env["PYTHONDONTWRITEBYTECODE"] = "1"
    run_env["PYTHONUNBUFFERED"] = "1"
    if env:
        run_env.update(env)

    start_time = time.perf_counter()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=run_env,
            stdin=subprocess.DEVNULL,
        )
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        stdout = _clip_output(proc.stdout)
        stderr = _clip_output(proc.stderr)
        if proc.returncode == 0:
            return {
                "command": str(command),
                "argv": argv,
                "status": "PASS",
                "exit_code": 0,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": duration_ms,
                "failure_code": None,
            }
        else:
            return {
                "command": str(command),
                "argv": argv,
                "status": "FAIL",
                "exit_code": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": duration_ms,
                "failure_code": "NONZERO_EXIT",
            }
    except FileNotFoundError as exc:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        return {
            "command": str(command),
            "argv": argv,
            "status": "FAIL",
            "exit_code": 127,
            "stdout": "",
            "stderr": f"Executable not found: {exc}",
            "duration_ms": duration_ms,
            "failure_code": "EXECUTABLE_NOT_FOUND",
        }
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        stdout = _clip_output(exc.stdout or "") if hasattr(exc, "stdout") else ""
        stderr = _clip_output(exc.stderr or "") if hasattr(exc, "stderr") else ""
        return {
            "command": str(command),
            "argv": argv,
            "status": "FAIL",
            "exit_code": 124,
            "stdout": stdout,
            "stderr": f"{stderr}\nVerification command timed out after {timeout}s".strip(),
            "duration_ms": duration_ms,
            "failure_code": "VERIFICATION_TIMEOUT",
        }
    except PermissionError as exc:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        return {
            "command": str(command),
            "argv": argv,
            "status": "FAIL",
            "exit_code": 126,
            "stdout": "",
            "stderr": f"Permission denied executing verification command: {exc}",
            "duration_ms": duration_ms,
            "failure_code": "PERMISSION_DENIED",
        }
    except Exception as exc:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        return {
            "command": str(command),
            "argv": argv,
            "status": "FAIL",
            "exit_code": 1,
            "stdout": "",
            "stderr": f"Unexpected error executing verification command: {exc}",
            "duration_ms": duration_ms,
            "failure_code": "UNEXPECTED_ERROR",
        }


def run_verification_suite(
    commands: list[Any],
    cwd: Path | str,
    timeout: float = 30.0,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not commands:
        return {
            "status": "PASS",
            "exit_code": 0,
            "command": "none",
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "failure_code": None,
            "command_results": [],
            "reason": "",
        }

    results: list[dict[str, Any]] = []
    total_duration_ms = 0

    for cmd in commands:
        res = run_single_verification(cmd, cwd=cwd, timeout=timeout, env=env)
        results.append(res)
        total_duration_ms += res.get("duration_ms", 0)

        if res["status"] != "PASS":
            cmd_display = res["command"] if isinstance(res["command"], str) else " ".join(res["argv"])
            return {
                "status": "FAIL",
                "exit_code": res["exit_code"],
                "command": cmd_display,
                "stdout": res["stdout"],
                "stderr": res["stderr"],
                "duration_ms": total_duration_ms,
                "failure_code": res.get("failure_code"),
                "command_results": results,
                "reason": res.get("failure_code") or "verification failed",
            }

    first_cmd = results[0]["command"] if results else "none"
    return {
        "status": "PASS",
        "exit_code": 0,
        "command": first_cmd,
        "stdout": results[-1]["stdout"] if results else "",
        "stderr": results[-1]["stderr"] if results else "",
        "duration_ms": total_duration_ms,
        "failure_code": None,
        "command_results": results,
        "reason": "",
    }
