from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from github_mcp.github_api import create_repository_api, whoami

mcp = MCPServer(
    "Ajan Ordusu GitHub",
    instructions=(
        "Safely manage GitHub repositories for the authenticated user. "
        "Only repository identity lookup and repository creation are exposed; "
        "repository deletion is intentionally not available."
    ),
)


@mcp.tool(
    annotations=ToolAnnotations(
        title="GitHub account check",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
def github_whoami() -> dict[str, Any]:
    """Return the GitHub account used by this MCP server without exposing its token."""
    user = whoami()
    return {
        "login": user.get("login"),
        "id": user.get("id"),
        "html_url": user.get("html_url"),
    }


@mcp.tool(
    annotations=ToolAnnotations(
        title="Create GitHub repository",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
)
def create_repository(
    name: str,
    private: bool = True,
    description: str = "",
    auto_init: bool = True,
) -> dict[str, Any]:
    """Create a repository for the authenticated GitHub user.

    This is an additive write action. It never deletes or overwrites a repository.
    Private repositories are the default. Public creation is separately gated.
    """
    return create_repository_api(name, private, description, auto_init)


def main() -> None:
    host = os.getenv("GITHUB_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("GITHUB_MCP_PORT", "8765"))
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
