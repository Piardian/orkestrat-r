from __future__ import annotations

import importlib
from typing import Any, Callable


_DEMO_DNS_SERVERS = ("1.1.1.1", "8.8.8.8")


def _inject_demo_dns(command: list[str]) -> list[str]:
    cmd = list(command)
    if len(cmd) < 2 or cmd[0] != "docker" or cmd[1] != "run":
        return cmd
    if "--dns" in cmd:
        return cmd

    dns_flags: list[str] = []
    for server in _DEMO_DNS_SERVERS:
        dns_flags.extend(["--dns", server])
    return cmd[:2] + dns_flags + cmd[2:]


def _install_dns_override() -> tuple[Any, Callable[..., Any]]:
    from openhands.workspace import DockerWorkspace

    module = importlib.import_module(DockerWorkspace.__module__)
    original = module.execute_command

    def execute_with_dns(command: list[str], *args: Any, **kwargs: Any) -> Any:
        return original(_inject_demo_dns(command), *args, **kwargs)

    module.execute_command = execute_with_dns
    return module, original


def main() -> int:
    module, original = _install_dns_override()
    try:
        from run_demo_goal_direct import main as direct_main

        return direct_main()
    finally:
        module.execute_command = original


if __name__ == "__main__":
    raise SystemExit(main())
