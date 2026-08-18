from __future__ import annotations

import os
from pathlib import Path
import shutil
import socket
import subprocess
from urllib.parse import urlparse

from orchestration.engine import resolve_openhands_python
from orchestration.temporal_flow import require_temporal_postgres


class ProductionPreflightError(RuntimeError):
    pass


def _truthy(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def run_production_preflight(*, orchestrator: str, core_dir: Path, openhands_python: str | None = None) -> dict[str, str]:
    """Fail before accepting work when required production dependencies are unavailable.

    This deliberately reuses each dependency's own client/CLI rather than inventing
    a second health system: Docker uses `docker info`, PostgreSQL uses psycopg,
    Temporal/LiteLLM/OTel endpoints use TCP readiness, and the isolated OpenHands
    interpreter proves that DockerWorkspace can be imported.
    """

    if not _truthy("AGENT_ARMY_PREFLIGHT_ENABLED", "true"):
        return {"preflight": "disabled"}

    checks: dict[str, str] = {}
    failures: list[str] = []

    if shutil.which("git") is None:
        failures.append("git executable not found")
    else:
        checks["git"] = "ok"

    if shutil.which("docker") is None:
        failures.append("Docker executable not found")
    else:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if proc.returncode != 0:
            failures.append("Docker daemon is not reachable")
        else:
            checks["docker"] = proc.stdout.strip() or "ok"

    python_exe = resolve_openhands_python(openhands_python, core_dir)
    probe = subprocess.run(
        [python_exe, "-c", "from openhands.workspace import DockerWorkspace; print('ok')"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if probe.returncode != 0:
        failures.append(f"OpenHands isolated interpreter is not ready: {python_exe}")
    else:
        checks["openhands"] = python_exe

    if orchestrator == "temporal":
        try:
            require_temporal_postgres()
        except Exception as exc:
            failures.append(str(exc))
        else:
            database_url = os.getenv("AGENT_ARMY_DATABASE_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
            try:
                import psycopg

                with psycopg.connect(database_url, connect_timeout=5) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                checks["postgres"] = "ok"
            except Exception as exc:
                failures.append(f"PostgreSQL is not ready: {exc}")

            temporal_address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
            if _tcp_ready(temporal_address, default_port=7233):
                checks["temporal"] = temporal_address
            else:
                failures.append(f"Temporal is not reachable at {temporal_address}")

    if _truthy("AGENT_ARMY_LITELLM_ENABLED", "false"):
        proxy = os.getenv("AGENT_ARMY_LITELLM_PROXY_URL", "http://127.0.0.1:4000/v1")
        parsed = urlparse(proxy)
        address = f"{parsed.hostname or '127.0.0.1'}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
        if _tcp_ready(address, default_port=4000):
            checks["litellm"] = address
        else:
            failures.append(f"LiteLLM gateway is not reachable at {address}")

    if _truthy("AGENT_ARMY_OTEL_ENABLED", "false"):
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
        parsed = urlparse(endpoint)
        address = f"{parsed.hostname or '127.0.0.1'}:{parsed.port or 4318}"
        if _tcp_ready(address, default_port=4318):
            checks["opentelemetry"] = address
        else:
            failures.append(f"OpenTelemetry Collector is not reachable at {address}")

    if failures:
        raise ProductionPreflightError("Production preflight failed: " + "; ".join(failures))
    checks["preflight"] = "pass"
    return checks


def _tcp_ready(address: str, *, default_port: int) -> bool:
    value = address.strip()
    if "://" in value:
        parsed = urlparse(value)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or default_port
    else:
        host, sep, raw_port = value.rpartition(":")
        if not sep:
            host, port = value, default_port
        else:
            port = int(raw_port)
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False
