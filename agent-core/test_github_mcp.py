from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from github_mcp import github_api


class GitHubMcpTests(unittest.TestCase):
    def test_rejects_unsafe_repository_name(self):
        with self.assertRaises(ValueError):
            github_api.validate_repo_name("bad/name")

    def test_rejects_unsafe_branch_name(self):
        for name in ("main*", "../main", "bad branch", "/main", "main.lock"):
            with self.assertRaises(ValueError):
                github_api.validate_branch_name(name)

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

    @patch.object(github_api, "request")
    def test_protect_branch_requires_pr_ci_and_blocks_force_push(self, request_mock):
        request_mock.side_effect = [
            {"login": "Piardian", "id": 1},
            {
                "required_status_checks": {"contexts": github_api.DEFAULT_REQUIRED_CHECKS},
                "enforce_admins": {"enabled": True},
                "required_pull_request_reviews": {"required_approving_review_count": 0},
                "allow_force_pushes": {"enabled": False},
                "allow_deletions": {"enabled": False},
                "required_conversation_resolution": {"enabled": True},
            },
        ]
        env = {
            github_api.EXPECTED_OWNER_ENV: "Piardian",
            github_api.TOKEN_ENV: "super-secret-token",
        }
        with patch.dict(os.environ, env, clear=False):
            result = github_api.protect_branch_api("orkestrat-r", "main")

        self.assertTrue(result["protected"])
        self.assertTrue(result["enforce_admins"])
        self.assertTrue(result["pull_request_required"])
        self.assertFalse(result["force_pushes_allowed"])
        self.assertFalse(result["deletions_allowed"])
        self.assertNotIn("super-secret-token", repr(result))

        method, path, payload = request_mock.call_args_list[1].args
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "/repos/Piardian/orkestrat-r/branches/main/protection")
        self.assertTrue(payload["required_status_checks"]["strict"])
        self.assertEqual(payload["required_status_checks"]["contexts"], github_api.DEFAULT_REQUIRED_CHECKS)
        self.assertTrue(payload["enforce_admins"])
        self.assertIsNotNone(payload["required_pull_request_reviews"])
        self.assertEqual(payload["required_pull_request_reviews"]["required_approving_review_count"], 0)
        self.assertFalse(payload["allow_force_pushes"])
        self.assertFalse(payload["allow_deletions"])

    @patch.object(github_api, "request")
    def test_protect_branch_owner_mismatch_stops_before_write(self, request_mock):
        request_mock.return_value = {"login": "SomeoneElse"}
        env = {
            github_api.EXPECTED_OWNER_ENV: "Piardian",
            github_api.TOKEN_ENV: "super-secret-token",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(RuntimeError):
                github_api.protect_branch_api("orkestrat-r")
        self.assertEqual(request_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
