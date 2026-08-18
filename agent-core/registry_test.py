from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

from registry import ProfileRegistry


def main() -> int:
    load_dotenv(dotenv_path=".env", override=False)
    registry = ProfileRegistry(Path("config") / "profiles.yaml")
    for profile in registry.list_profiles():
        print(
            f"{profile['id']} provider={profile['provider']} "
            f"model={profile['model']} secret={profile['secret']} "
            f"owner={profile['owner']} role={profile['role']}"
        )
    nemotron = registry.get("nemotron-main")
    configuration_ready = (
        nemotron.provider == "openai-compatible"
        and bool(nemotron.model)
        and bool(nemotron.base_url)
        and bool(nemotron.secret_env)
    )
    print("\nNEMOTRON STATUS")
    print(f"Profile: {nemotron.id}")
    print(f"Provider: {nemotron.provider}")
    print(f"Configuration: {'READY' if configuration_ready else 'INCOMPLETE'}")
    print(f"API key: {'CONFIGURED' if nemotron.secret_configured else 'MISSING'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
