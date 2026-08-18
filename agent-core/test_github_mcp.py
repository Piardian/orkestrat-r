from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from github_mcp import github_api


class GitHubMcpTests(unittest.TestCase):
    def test_rejects_unsafe_repository_name(self):
        with self.assertRaises(ValueError):
            github_api.validate_repo_name("bad/name")

    @patch.object(github_api, "request")
    def test_create_is_disabled_by_default(self, request_mock):
        with patch.dict(os.environ, {github_api.ALLOW_CREATE_ENV: "false"}, clear=False):
            with self.assertRaises(RuntimeError):
                github_api.create_repository_api("safe-repo")
        request_mock.assert_not_called()

    @patch.object(github_api, "request")
    def test_create_uses_private_default_and_never_returns_token(self, request_mock):
        request_mock.side_effect = [
            {"login": "Piardian", "id": 1, "html_url": "https://github.com/Piardian"},
            {
                "name": "safe-repo",
                "full_name": "Piardian/safe-repo",
                "private": True,
                "default_branch": "main",
                "html_url": "https://github.com/Piardian/safe-repo",
                "clone_url": "https://github.com/Piardian/safe-repo.git",
            },
        ]
        env = {
            github_api.ALLOW_CREATE_ENV: "true",
            github_api.EXPECTED_OWNER_ENV: "Piardian",
            github_api.TOKEN_ENV: "super-secret-token",
        }
        with patch.dict(os.environ, env, clear=False):
            result = github_api.create_repository_api("safe-repo")

        self.assertTrue(result["created"])
        self.assertTrue(result["private"])
        self.assertNotIn("super-secret-token", repr(result))
        method, path, payload = request_mock.call_args_list[1].args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/user/repos")
        self.assertTrue(payload["private"])

    @patch.object(github_api, "request")
    def test_owner_mismatch_blocks_creation(self, request_mock):
        request_mock.return_value = {"login": "SomeoneElse"}
        env = {
            github_api.ALLOW_CREATE_ENV: "true",
            github_api.EXPECTED_OWNER_ENV: "Piardian",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                github_api.create_repository_api("safe-repo")
        self.assertEqual(request_mock.call_count, 1)

    @patch.object(github_api, "request")
    def test_public_creation_is_separately_blocked(self, request_mock):
        env = {
            github_api.ALLOW_CREATE_ENV: "true",
            github_api.ALLOW_PUBLIC_ENV: "false",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                github_api.create_repository_api("public-repo", private=False)
        request_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
