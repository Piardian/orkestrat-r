from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any

GITHUB_API_BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
TOKEN_ENV = "GITHUB_MCP_TOKEN"
ALLOW_CREATE_ENV = "GITHUB_MCP_ALLOW_CREATE"
EXPECTED_OWNER_ENV = "GITHUB_MCP_ALLOWED_OWNER"
ALLOW_PUBLIC_ENV = "GITHUB_MCP_ALLOW_PUBLIC"
API_VERSION = os.getenv("GITHUB_API_VERSION", "2022-11-28")


def token() -> str:
    value = os.getenv(TOKEN_ENV, "").strip()
    if not value:
        raise RuntimeError(
            f"{TOKEN_ENV} is not configured. Store the GitHub token only in the local environment, never in git."
        )
    return value


def headers() -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token()}",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "ajan-ordusu-github-mcp/1.0",
        "Content-Type": "application/json",
    }


def request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{GITHUB_API_BASE}{path}",
        data=body,
        headers=headers(),
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
            message = parsed.get("message") or detail
        except json.JSONDecodeError:
            message = detail or str(exc)
        accepted = exc.headers.get("X-Accepted-GitHub-Permissions", "") if exc.headers else ""
        suffix = f" Required/accepted permissions: {accepted}" if accepted else ""
        raise RuntimeError(f"GitHub API error {exc.code}: {message}.{suffix}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"GitHub API connection failed: {exc.reason}") from exc


def validate_repo_name(name: str) -> str:
    clean = name.strip()
    if not clean or len(clean) > 100:
        raise ValueError("Repository name must contain 1-100 characters.")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", clean):
        raise ValueError("Repository name may use only letters, numbers, '.', '_' and '-'.")
    return clean


def whoami() -> dict[str, Any]:
    user = request("GET", "/user")
    expected_owner = os.getenv(EXPECTED_OWNER_ENV, "").strip()
    login = str(user.get("login") or "")
    if expected_owner and login.casefold() != expected_owner.casefold():
        raise RuntimeError(
            f"GitHub token belongs to '{login}', but {EXPECTED_OWNER_ENV} requires '{expected_owner}'."
        )
    return user


def create_repository_api(
    name: str,
    private: bool = True,
    description: str = "",
    auto_init: bool = True,
) -> dict[str, Any]:
    if os.getenv(ALLOW_CREATE_ENV, "false").strip().lower() != "true":
        raise RuntimeError(
            f"Repository creation is disabled. Set {ALLOW_CREATE_ENV}=true in the local environment to enable it."
        )

    if not private and os.getenv(ALLOW_PUBLIC_ENV, "false").strip().lower() != "true":
        raise RuntimeError(
            f"Public repository creation is disabled. Set {ALLOW_PUBLIC_ENV}=true only when you explicitly want public repositories."
        )

    repo_name = validate_repo_name(name)
    user = whoami()
    payload = {
        "name": repo_name,
        "description": description.strip()[:350],
        "private": bool(private),
        "auto_init": bool(auto_init),
    }
    repo = request("POST", "/user/repos", payload)
    return {
        "created": True,
        "owner": user.get("login"),
        "name": repo.get("name"),
        "full_name": repo.get("full_name"),
        "private": repo.get("private"),
        "default_branch": repo.get("default_branch"),
        "html_url": repo.get("html_url"),
        "clone_url": repo.get("clone_url"),
    }
