from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
import unittest

from goal import (
    BuilderRequest,
    BuilderResult,
    GoalBuilderService,
    GoalComplexityService,
    GoalPlan,
    GoalReview,
    GoalService,
    GoalStore,
)
from goal.complexity import ComplexityAssessment, ComplexityFactor
from schemas import Review, SearchPlan, Verdict


class FakeBuilderAdapter:
    def __init__(self, result: BuilderResult) -> None:
        self.result = result
        self.calls = 0
        self.last_request: BuilderRequest | None = None

    def execute(self, request: BuilderRequest) -> BuilderResult:
        self.calls += 1
        self.last_request = request
        return self.result


class Phase5GoalTests(unittest.TestCase):
    def test_dry_run_writes_nothing_and_reports_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_ready_goal(root, repo)
            builder = GoalBuilderService(service=service, runtime_root=root / "runtime" / "workspaces")
            record, request = builder.dry_run(approved.goal_id)
            self.assertEqual(record.status, "READY_FOR_OPENHANDS")
            self.assertEqual(request.mode, "local-safe")
            self.assertFalse((service.store.goal_dir(approved.goal_id) / "build_request.json").exists())
            self.assertFalse((service.store.goal_dir(approved.goal_id) / "build.json").exists())

    def test_ready_goal_builds_to_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_ready_goal(root, repo, candidate_files=["app.py"])
            patch_path = root / "build.patch"
            patch_path.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
            result = BuilderResult(
                goal_id=approved.goal_id,
                status="BUILT_PENDING_REVIEW",
                failure_type=None,
                recommended_executor="openhands",
                changed_files=["app.py"],
                unauthorized_files=[],
                patch_path=str(patch_path),
                verification_status="PASS",
                verification_commands=["py -m unittest"],
                verification_result={"status": "PASS", "exit_code": 0},
                openhands_executed=True,
                terminal_tool_enabled=False,
                original_repo_modified=False,
            )
            builder = GoalBuilderService(service=service, adapter=FakeBuilderAdapter(result), runtime_root=root / "runtime" / "workspaces")
            record, request, built = builder.execute(approved.goal_id)
            self.assertEqual(record.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(built.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(built.verification_status, "PASS")
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "build_request.json").exists())
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "build.json").exists())
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "verification.json").exists())
            self.assertTrue(request.allowed_files)

    def test_builder_service_persists_success_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_ready_goal(root, repo, candidate_files=["calculator.py"])
            patch_path = root / "build.patch"
            patch_path.write_text("diff --git a/calculator.py b/calculator.py\n", encoding="utf-8")
            result = BuilderResult(
                goal_id=approved.goal_id,
                status="BUILT_PENDING_REVIEW",
                failure_type=None,
                recommended_executor="openhands",
                changed_files=["calculator.py"],
                unauthorized_files=[],
                patch_path=str(patch_path),
                verification_status="PASS",
                verification_commands=["py -m unittest"],
                verification_result={"status": "PASS", "exit_code": 0},
                openhands_executed=True,
                terminal_tool_enabled=False,
                original_repo_modified=False,
            )
            builder = GoalBuilderService(service=service, adapter=FakeBuilderAdapter(result), runtime_root=root / "runtime" / "workspaces")
            record, _, built = builder.execute(approved.goal_id)
            persisted = service.store.load(approved.goal_id)
            history = service.store.read_jsonl(approved.goal_id, "history.jsonl")
            self.assertEqual(record.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(built.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(persisted.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(history[-1]["to"], "BUILT_PENDING_REVIEW")
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "build.patch").exists())

    def test_builder_service_creates_real_git_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_repo = self._init_git_repo(root / "parent")
            nested_repo = parent_repo / "target"
            nested_repo.mkdir()
            self._git(nested_repo, "init")
            self._git(nested_repo, "config", "user.email", "test@example.com")
            self._git(nested_repo, "config", "user.name", "Test User")
            (nested_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (nested_repo / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
            self._git(nested_repo, "add", ".")
            self._git(nested_repo, "commit", "-m", "init target")

            service, approved = self._seed_ready_goal(root, nested_repo, candidate_files=["calculator.py"])
            runtime_root = root / "runtime" / "workspaces"
            builder = GoalBuilderService(service=service, adapter=self._fake_worktree_adapter(root), runtime_root=runtime_root)
            record, request, built = builder.execute(approved.goal_id)
            workspace = Path(request.workspace_path)

            self.assertEqual(record.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(built.status, "BUILT_PENDING_REVIEW")
            self.assertTrue((workspace / ".git").exists() or (workspace / ".git").is_file())
            self.assertTrue(workspace.exists())
            top_level = self._git_output(workspace, "rev-parse", "--show-toplevel")
            self.assertEqual(Path(top_level.strip()).resolve(), workspace.resolve())
            head_calculator = self._git_output(workspace, "show", "HEAD:calculator.py")
            self.assertIn("def add", head_calculator)
            diff = self._git_output(workspace, "diff", "--", "calculator.py")
            self.assertTrue(diff.strip())
            self.assertTrue((service.store.goal_dir(approved.goal_id) / "build.patch").exists())
            self.assertEqual((nested_repo / "calculator.py").read_text(encoding="utf-8"), "def add(a, b):\n    return a + b\n")

    def test_builder_service_rejects_inherited_parent_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_repo = self._init_git_repo(root / "parent")
            nested_repo = parent_repo / "target"
            nested_repo.mkdir()
            self._git(nested_repo, "init")
            self._git(nested_repo, "config", "user.email", "test@example.com")
            self._git(nested_repo, "config", "user.name", "Test User")
            (nested_repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            (nested_repo / "test_calculator.py").write_text("import unittest\n", encoding="utf-8")
            self._git(nested_repo, "add", ".")
            self._git(nested_repo, "commit", "-m", "init target")

            service, approved = self._seed_ready_goal(root, nested_repo, candidate_files=["calculator.py"])
            builder = GoalBuilderService(service=service, adapter=self._fake_worktree_adapter(root), runtime_root=root / "runtime" / "workspaces")
            record, request, built = builder.execute(approved.goal_id)
            workspace = Path(request.workspace_path)
            self.assertEqual(record.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(built.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(Path(self._git_output(workspace, "rev-parse", "--show-toplevel").strip()).resolve(), workspace.resolve())

    def _fake_worktree_adapter(self, root: Path) -> FakeBuilderAdapter:
        class _Adapter(FakeBuilderAdapter):
            def execute(self, request: BuilderRequest) -> BuilderResult:
                workspace = Path(request.workspace_path)
                (workspace / "calculator.py").write_text(
                    "def add(a: int, b: int) -> int:\n    return a + b\n",
                    encoding="utf-8",
                )
                patch_path = workspace / "build.patch"
                patch_path.write_text("diff --git a/calculator.py b/calculator.py\n", encoding="utf-8")
                self.result = BuilderResult(
                    goal_id=request.goal_id,
                    status="BUILT_PENDING_REVIEW",
                    failure_type=None,
                    recommended_executor="openhands",
                    changed_files=["calculator.py"],
                    unauthorized_files=[],
                    patch_path=str(patch_path),
                    verification_status="PASS",
                    verification_commands=["py -m unittest"],
                    verification_result={"status": "PASS", "exit_code": 0},
                    openhands_executed=True,
                    terminal_tool_enabled=False,
                    original_repo_modified=False,
                )
                return super().execute(request)

        return _Adapter(
            BuilderResult(
                goal_id="GOAL-TEST",
                status="BUILT_PENDING_REVIEW",
                failure_type=None,
                recommended_executor="openhands",
                changed_files=["calculator.py"],
                unauthorized_files=[],
                patch_path=str(root / "build.patch"),
                verification_status="PASS",
                verification_commands=["py -m unittest"],
                verification_result={"status": "PASS", "exit_code": 0},
                openhands_executed=True,
                terminal_tool_enabled=False,
                original_repo_modified=False,
            )
        )

    def test_unapproved_goal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Build login", repo)
            builder = GoalBuilderService(service=service, runtime_root=root / "runtime" / "workspaces")
            with self.assertRaises(ValueError):
                builder.execute(created.goal_id)

    def test_non_git_target_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            service, approved = self._seed_ready_goal(root, repo)
            builder = GoalBuilderService(service=service, adapter=FakeBuilderAdapter(self._fake_result(approved.goal_id)), runtime_root=root / "runtime" / "workspaces")
            with self.assertRaises(RuntimeError):
                builder.execute(approved.goal_id)
            self.assertEqual(service.store.load(approved.goal_id).status, "BUILDER_BLOCKED")

    def test_dirty_git_target_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            (repo / "app.py").write_text("print('dirty')\n", encoding="utf-8")
            self._git(repo, "add", "app.py")
            service, approved = self._seed_ready_goal(root, repo)
            builder = GoalBuilderService(service=service, adapter=FakeBuilderAdapter(self._fake_result(approved.goal_id)), runtime_root=root / "runtime" / "workspaces")
            with self.assertRaises(RuntimeError):
                builder.execute(approved.goal_id)
            self.assertEqual(service.store.load(approved.goal_id).status, "BUILDER_BLOCKED")

    def test_relaxed_acceptance_dirty_git_target_warns_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            (repo / "app.py").write_text("print('dirty')\n", encoding="utf-8")
            self._git(repo, "add", "app.py")
            service, approved = self._seed_ready_goal(root, repo)
            patch_path = root / "build.patch"
            patch_path.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
            result = BuilderResult(
                goal_id=approved.goal_id,
                status="BUILT_PENDING_REVIEW",
                failure_type=None,
                recommended_executor="openhands",
                changed_files=["app.py"],
                unauthorized_files=[],
                patch_path=str(patch_path),
                verification_status="PASS",
                verification_result={"status": "PASS", "exit_code": 0},
                openhands_executed=True,
                terminal_tool_enabled=False,
                original_repo_modified=False,
                preflight_warnings=[],
            )
            builder = GoalBuilderService(
                service=service,
                adapter=FakeBuilderAdapter(result),
                execution_mode="relaxed-acceptance",
                runtime_root=root / "runtime" / "workspaces",
            )
            record, request, built = builder.execute(approved.goal_id)
            self.assertEqual(record.status, "BUILT_PENDING_REVIEW")
            self.assertEqual(built.status, "BUILT_PENDING_REVIEW")
            self.assertTrue(built.preflight_warnings)
            self.assertEqual(built.preflight_warnings[0]["code"], "DIRTY_TARGET_REPO")
            saved = json.loads((service.store.goal_dir(approved.goal_id) / "build.json").read_text(encoding="utf-8"))
            self.assertTrue(saved["preflight_warnings"])
            self.assertEqual(saved["preflight_warnings"][0]["mode"], "relaxed-acceptance")

    def test_relaxed_acceptance_path_escape_attempt_still_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_ready_goal(root, repo)
            builder = GoalBuilderService(service=service, adapter=FakeBuilderAdapter(self._fake_result(approved.goal_id)), execution_mode="relaxed-acceptance", runtime_root=root / "runtime" / "workspaces")
            request = builder.dry_run(approved.goal_id)[1]
            self.assertTrue(all(".." not in item for item in request.allowed_files))

    def test_unauthorized_file_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_ready_goal(root, repo, candidate_files=["app.py"])
            patch_path = root / "build.patch"
            patch_path.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
            result = BuilderResult(
                goal_id=approved.goal_id,
                status="BUILT_PENDING_REVIEW",
                failure_type=None,
                recommended_executor="openhands",
                changed_files=["app.py", "other.py"],
                unauthorized_files=["other.py"],
                patch_path=str(patch_path),
                verification_status="PASS",
                verification_result={"status": "PASS"},
                openhands_executed=True,
            )
            builder = GoalBuilderService(service=service, adapter=FakeBuilderAdapter(result), runtime_root=root / "runtime" / "workspaces")
            record, request, built = builder.execute(approved.goal_id)
            self.assertEqual(record.status, "BUILDER_POLICY_VIOLATION")
            self.assertEqual(built.status, "BUILT_PENDING_REVIEW")
            self.assertIn("app.py", request.allowed_files)

    def test_verification_fail_blocks_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            service, approved = self._seed_ready_goal(root, repo, candidate_files=["app.py"])
            patch_path = root / "build.patch"
            patch_path.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
            result = BuilderResult(
                goal_id=approved.goal_id,
                status="BUILT_PENDING_REVIEW",
                failure_type=None,
                recommended_executor="openhands",
                changed_files=["app.py"],
                unauthorized_files=[],
                patch_path=str(patch_path),
                verification_status="FAIL",
                verification_result={"status": "FAIL", "exit_code": 1},
                openhands_executed=True,
            )
            builder = GoalBuilderService(service=service, adapter=FakeBuilderAdapter(result), runtime_root=root / "runtime" / "workspaces")
            record, _, built = builder.execute(approved.goal_id)
            self.assertEqual(record.status, "BUILD_FAILED")
            self.assertEqual(built.verification_status, "FAIL")

    def test_target_repo_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._init_git_repo(root / "repo")
            repo_file = repo / "app.py"
            repo_file.write_text("print('before')\n", encoding="utf-8")
            self._git(repo, "add", "app.py")
            before = repo_file.read_text(encoding="utf-8")
            service, approved = self._seed_ready_goal(root, repo, candidate_files=["app.py"])
            patch_path = root / "build.patch"
            patch_path.write_text("diff --git a/app.py b/app.py\n", encoding="utf-8")
            result = BuilderResult(
                goal_id=approved.goal_id,
                status="BUILT_PENDING_REVIEW",
                failure_type=None,
                recommended_executor="openhands",
                changed_files=["app.py"],
                unauthorized_files=[],
                patch_path=str(patch_path),
                verification_status="PASS",
                verification_result={"status": "PASS"},
                openhands_executed=True,
            )
            GoalBuilderService(service=service, adapter=FakeBuilderAdapter(result), runtime_root=root / "runtime" / "workspaces").execute(approved.goal_id)
            after = repo_file.read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def _seed_ready_goal(
        self,
        root: Path,
        repo: Path,
        candidate_files: list[str] | None = None,
    ):
        candidate_files = candidate_files or ["app.py"]
        store = GoalStore(root / "runtime" / "goals")
        service = GoalService(store)
        created = service.create_goal("Build login", repo)
        planning = service.update_status(created, "PLANNING", phase="planning")
        planned = service.update_status(planning, "PLANNED", phase="planned")
        reviewing = service.update_status(planned, "REVIEWING", phase="reviewing")
        approved = service.update_status(reviewing, "APPROVED", phase="approved")
        search_plan = SearchPlan(task=approved.goal, search_terms=["login"])
        evidence = {"task": approved.goal, "repository": str(repo), "summary": {}, "evidence": []}
        plan = GoalPlan(
            plan_version=1,
            goal_id=approved.goal_id,
            objective=approved.goal,
            summary="small change",
            tasks=[{"id": "TASK-1", "title": "Small change", "description": "Adjust one function", "depends_on": []}],
            candidate_files=candidate_files,
            acceptance_criteria=["works"],
            verification=["py -m unittest"],
            risks=[],
            constraints=["No secrets"],
            patch_expected=True,
            uncertainties=[],
            evidence_refs=[],
        )
        review = Review(
            final_verdict="PASS",
            confidence=0.95,
            agreement="FULL",
            reason="Looks good.",
            analyst_a={"verdict": "PASS"},
            analyst_b={"verdict": "PASS"},
            analysts=[{"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "PASS"}],
            evidence=[],
            patch_required=False,
            risk_flags=[],
        )
        analyst_payload = [
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[], analyst="analyst-1", profile="gemini-user-b", uncertainties=[], risk_flags=[]).to_dict(),
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[], analyst="analyst-2", profile="gemini-user-c", uncertainties=[], risk_flags=[]).to_dict(),
            Verdict(verdict="PASS", confidence=0.9, reason="ok", evidence=[], analyst="analyst-3", profile="gemini-user-a", uncertainties=[], risk_flags=[]).to_dict(),
        ]
        complexity = ComplexityAssessment(
            version=1,
            goal_id=approved.goal_id,
            score=0,
            severity="EASY",
            recommended_executor="openhands",
            factors=[ComplexityFactor(name="candidate_files", score=0, reason="1 candidate file")],
            hard_overrides=[],
            candidate_file_count=len(candidate_files),
            module_count=1,
            review_risk_count=0,
            llm_calls=0,
        )
        store.save_plan_bundle(approved, search_plan.to_dict(), evidence, plan)
        store.save_review_bundle(approved, analyst_payload, GoalReview(
            goal_id=approved.goal_id,
            task=approved.goal,
            reviewer_profile="gemini-user-d",
            status="APPROVED",
            agreement=review.agreement,
            final_verdict=review.final_verdict,
            confidence=review.confidence,
            reason=review.reason,
            patch_required=review.patch_required,
            analyst_results=analyst_payload,
            reviewer_result=review.to_dict(),
            evidence_refs=[],
            provider_requests=4,
            logical_calls=4,
            provider_retries=0,
            json_repairs=0,
            stage_regenerations=0,
            input_tokens=40,
            output_tokens=32,
        ))
        GoalComplexityService(service=service).assess_goal(approved.goal_id)
        return service, approved

    def _init_git_repo(self, path: Path) -> Path:
        path.mkdir(parents=True, exist_ok=True)
        self._git(path, "init")
        self._git(path, "config", "user.email", "test@example.com")
        self._git(path, "config", "user.name", "Test User")
        (path / "app.py").write_text("print('before')\n", encoding="utf-8")
        self._git(path, "add", ".")
        self._git(path, "commit", "-m", "init")
        return path

    def _git(self, path: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)

    def _git_output(self, path: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)
        return result.stdout

    def _fake_result(self, goal_id: str) -> BuilderResult:
        return BuilderResult(
            goal_id=goal_id,
            status="BUILT_PENDING_REVIEW",
            failure_type=None,
            recommended_executor="openhands",
            changed_files=["app.py"],
            unauthorized_files=[],
            patch_path=None,
            verification_status="PASS",
            verification_result={"status": "PASS", "exit_code": 0},
            openhands_executed=True,
            terminal_tool_enabled=False,
            original_repo_modified=False,
        )


if __name__ == "__main__":
    unittest.main()
