from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GITHUB_API_BASE = os.getenv("GITHUB_API_BASE", "https://api.github.com").rstrip("/")
TOKEN_ENV = "GITHUB_MCP_TOKEN"
ALLOW_CREATE_ENV = "GITHUB_MCP_ALLOW_CREATE"
EXPECTED_OWNER_ENV = "GITHUB_MCP_ALLOWED_OWNER"
ALLOW_PUBLIC_ENV = "GITHUB_MCP_ALLOW_PUBLIC"
API_VERSION = os.getenv("GITHUB_API_VERSION", "2022-11-28")

DEFAULT_REQUIRED_CHECKS = [
    "full-regression",
    "reliability-smoke (ubuntu-latest, 3.11)",
    "reliability-smoke (ubuntu-latest, 3.12)",
    "reliability-smoke (windows-latest, 3.11)",
    "reliability-smoke (windows-latest, 3.12)",
]


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


def validate_branch_name(name: str) -> str:
    clean = name.strip()
    if not clean or len(clean) > 255:
        raise ValueError("Branch name must contain 1-255 characters.")
    if any(token in clean for token in ("*", "?", "[", "]", " ", "..")):
        raise ValueError("Branch name contains unsupported wildcard, space, or traversal characters.")
    if clean.startswith(("/", ".")) or clean.endswith(("/", ".", ".lock")) or "//" in clean:
        raise ValueError("Branch name is not safe.")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", clean):
        raise ValueError("Branch name contains unsupported characters.")
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


def protect_branch_api(
    repo_name: str,
    branch: str = "main",
    required_checks: list[str] | None = None,
) -> dict[str, Any]:
    """Strengthen branch protection without exposing any unprotect/delete operation."""
    repo = validate_repo_name(repo_name)
    branch_name = validate_branch_name(branch)
    user = whoami()
    owner = str(user.get("login") or "")
    checks = [str(item).strip() for item in (required_checks or DEFAULT_REQUIRED_CHECKS) if str(item).strip()]
    if not checks:
        raise ValueError("At least one required status check is required.")
    if len(checks) > 50 or any(len(item) > 200 for item in checks):
        raise ValueError("Required status check list is too large.")

    payload: dict[str, Any] = {
        "required_status_checks": {
            "strict": True,
            "contexts": checks,
        },
        "enforce_admins": True,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": True,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 0,
            "require_last_push_approval": False,
        },
        "restrictions": None,
        "required_linear_history": False,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "block_creations": False,
        "required_conversation_resolution": True,
        "lock_branch": False,
        "allow_fork_syncing": False,
    }
    encoded_owner = urllib.parse.quote(owner, safe="")
    encoded_repo = urllib.parse.quote(repo, safe="")
    encoded_branch = urllib.parse.quote(branch_name, safe="")
    result = request(
        "PUT",
        f"/repos/{encoded_owner}/{encoded_repo}/branches/{encoded_branch}/protection",
        payload,
    )
    returned_checks = ((result.get("required_status_checks") or {}).get("contexts") or checks)
    return {
        "protected": True,
        "owner": owner,
        "repo": repo,
        "branch": branch_name,
        "strict_status_checks": True,
        "required_checks": list(returned_checks),
        "enforce_admins": bool((result.get("enforce_admins") or {}).get("enabled", True)),
        "pull_request_required": result.get("required_pull_request_reviews") is not None,
        "force_pushes_allowed": bool((result.get("allow_force_pushes") or {}).get("enabled", False)),
        "deletions_allowed": bool((result.get("allow_deletions") or {}).get("enabled", False)),
        "conversation_resolution_required": bool(
            (result.get("required_conversation_resolution") or {}).get("enabled", True)
        ),
    }
