from __future__ import annotations

import json
import tempfile
from pathlib import Path
import unittest
from io import BytesIO
from unittest.mock import patch
import urllib.error

from llm.client import BaseLLMClient, GeminiClient, LLMError, LLMResponse
from goal import GoalService, GoalStore
from goal.metrics_service import GoalMetricsService
from goal.plan import GoalPlan
from goal.review_service import GoalReviewService
from schemas import Review, SearchPlan, Verdict


class StaticClient(BaseLLMClient):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def generate(self, system_prompt: str, user_prompt: str, **options):
        self.calls += 1
        return LLMResponse(
            text=json.dumps(self.payload, ensure_ascii=False),
            input_tokens=10,
            output_tokens=8,
            retry_count=0,
            finish_reason="STOP",
        )


class CapturingClient(BaseLLMClient):
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0
        self.system_prompts: list[str] = []
        self.user_prompts: list[str] = []

    def generate(self, system_prompt: str, user_prompt: str, **options):
        self.calls += 1
        self.system_prompts.append(system_prompt)
        self.user_prompts.append(user_prompt)
        return LLMResponse(
            text=json.dumps(self.payload, ensure_ascii=False),
            input_tokens=10,
            output_tokens=8,
            retry_count=0,
            finish_reason="STOP",
        )


class BrokenClient(BaseLLMClient):
    def generate(self, system_prompt: str, user_prompt: str, **options):
        raise RuntimeError("boom")


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str = "{}") -> None:
        super().__init__("https://example.test", code, "err", hdrs=None, fp=BytesIO(body.encode("utf-8")))
        self._body = body

    def read(self) -> bytes:  # type: ignore[override]
        return self._body.encode("utf-8")


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeRouter:
    def __init__(self, clients: dict[str, BaseLLMClient]) -> None:
        self.clients = clients

    def get_client(self, profile_id: str):
        return self.clients[profile_id]


class Phase3GoalTests(unittest.TestCase):
    def test_goal_review_creates_review_bundle_and_final_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Build JWT login", repo)
            planning = service.update_status(created, "PLANNING", phase="planning")
            planned = service.update_status(planning, "PLANNED", phase="planned")
            search_plan = SearchPlan(task=planned.goal, search_terms=["jwt", "login"])
            evidence = {
                "task": planned.goal,
                "repository": str(repo),
                "summary": {"files_inspected": 1, "lines_captured": 1},
                "evidence": [{"path": "server/auth.ts", "line_start": 1, "line_end": 1}],
            }
            plan = GoalPlan(
                plan_version=1,
                goal_id=planned.goal_id,
                objective=planned.goal,
                summary="Implement JWT login flow.",
                tasks=[{"id": "TASK-001", "title": "Add auth route", "description": "Create auth route", "depends_on": []}],
                candidate_files=["server/auth.ts"],
                acceptance_criteria=["Login works"],
                verification=["run tests"],
                risks=["JWT expiry"],
                constraints=["No secret leak"],
                patch_expected=True,
                uncertainties=[],
                evidence_refs=["server/auth.ts:1-1"],
            )
            store.save_plan_bundle(planned, search_plan.to_dict(), evidence, plan)

            clients = {
                "gemini-user-b": StaticClient(_verdict_payload("analyst-1", "gemini-user-b", "PASS")),
                "gemini-user-c": StaticClient(_verdict_payload("analyst-2", "gemini-user-c", "PASS")),
                "gemini-user-a": StaticClient(_verdict_payload("analyst-3", "gemini-user-a", "UNKNOWN")),
                "gemini-user-d": StaticClient(
                    {
                        "final_verdict": "PASS",
                        "confidence": 0.9,
                        "agreement": "FULL",
                        "reason": "Looks good.",
                        "analyst_a": {"verdict": "PASS"},
                        "analyst_b": {"verdict": "PASS"},
                        "analysts": [{"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "UNKNOWN"}],
                        "evidence": [{"path": "server/auth.ts", "lines": "1-1"}],
                        "patch_required": False,
                    }
                ),
            }
            review_service = GoalReviewService(service=service, router=FakeRouter(clients))
            updated, analysts, review, goal_review, usage = review_service.review_goal(planned.goal_id)

            self.assertEqual(updated.status, "APPROVED")
            self.assertEqual(goal_review.status, "APPROVED")
            self.assertEqual(len(analysts), 3)
            self.assertEqual(usage["provider_requests"], 4)
            self.assertEqual(goal_review.logical_calls, 4)
            self.assertTrue((store.goal_dir(planned.goal_id) / "review.json").exists())
            self.assertTrue((store.goal_dir(planned.goal_id) / "analyst-1-review.json").exists())
            self.assertTrue((store.goal_dir(planned.goal_id) / "analyst-2-review.json").exists())
            self.assertTrue((store.goal_dir(planned.goal_id) / "analyst-3-review.json").exists())

    def test_goal_review_requires_planned_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(Path(tmp) / "runtime" / "goals"))
            created = service.create_goal("Build JWT login", repo)
            review_service = GoalReviewService(service=service, router=FakeRouter({}))
            with self.assertRaises(ValueError):
                review_service.review_goal(created.goal_id)

    def test_goal_review_marks_failure_on_broken_analyst(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Build JWT login", repo)
            planning = service.update_status(created, "PLANNING", phase="planning")
            planned = service.update_status(planning, "PLANNED", phase="planned")
            store.save_plan_bundle(
                planned,
                SearchPlan(task=planned.goal, search_terms=["jwt"]).to_dict(),
                {"task": planned.goal, "repository": str(repo), "summary": {}, "evidence": []},
                GoalPlan(
                    plan_version=1,
                    goal_id=planned.goal_id,
                    objective=planned.goal,
                    summary="Implement JWT login flow.",
                    tasks=[],
                    candidate_files=[],
                    acceptance_criteria=[],
                    verification=[],
                    risks=[],
                    constraints=[],
                    patch_expected=True,
                    uncertainties=[],
                    evidence_refs=[],
                ),
            )
            clients = {
                "gemini-user-b": BrokenClient(),
                "gemini-user-c": StaticClient(_verdict_payload("analyst-2", "gemini-user-c", "PASS")),
                "gemini-user-a": StaticClient(_verdict_payload("analyst-3", "gemini-user-a", "PASS")),
                "gemini-user-d": StaticClient({"final_verdict": "PASS", "confidence": 1.0, "agreement": "FULL", "reason": "", "analysts": [], "evidence": [], "patch_required": False}),
            }
            review_service = GoalReviewService(service=service, router=FakeRouter(clients))
            with self.assertRaises(RuntimeError):
                review_service.review_goal(planned.goal_id)
            loaded = store.load(planned.goal_id)
            self.assertEqual(loaded.status, "REVIEW_FAILED")

    def test_goal_review_retries_503_and_persists_failure_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Build JWT login", repo)
            planning = service.update_status(created, "PLANNING", phase="planning")
            planned = service.update_status(planning, "PLANNED", phase="planned")
            store.save_plan_bundle(
                planned,
                SearchPlan(task=planned.goal, search_terms=["jwt"]).to_dict(),
                {"task": planned.goal, "repository": str(repo), "summary": {}, "evidence": [{"path": "server/auth.ts", "line_start": 1, "line_end": 1}]},
                GoalPlan(
                    plan_version=1,
                    goal_id=planned.goal_id,
                    objective=planned.goal,
                    summary="Implement JWT login flow.",
                    tasks=[],
                    candidate_files=["server/auth.ts"],
                    acceptance_criteria=["Login works"],
                    verification=["run tests"],
                    risks=["JWT expiry"],
                    constraints=["No secret leak"],
                    patch_expected=True,
                    uncertainties=[],
                    evidence_refs=["server/auth.ts:1-1"],
                ),
            )

            calls = {"count": 0}

            def fake_urlopen(request, timeout=None):  # noqa: ANN001
                calls["count"] += 1
                if calls["count"] == 1:
                    raise _FakeHTTPError(503, '{"error":{"message":"temporarily unavailable"}}')
                if calls["count"] in {2, 3, 4}:
                    payload = {
                        "candidates": [{"content": {"parts": [{"text": _analyst_text(calls["count"])}]}, "finishReason": "STOP"}],
                        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                    }
                    return _FakeResponse(payload)
                payload = {
                    "candidates": [{"content": {"parts": [{"text": _review_text()}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
                }
                return _FakeResponse(payload)

            with patch("llm.client.urllib.request.urlopen", side_effect=fake_urlopen), patch("llm.client.time.sleep"):
                clients = {
                    "gemini-user-b": GeminiClient("gemini-3.5-flash-lite", "key", max_retries=2),
                    "gemini-user-c": GeminiClient("gemini-3.5-flash-lite", "key", max_retries=2),
                    "gemini-user-a": GeminiClient("gemini-3.5-flash-lite", "key", max_retries=2),
                    "gemini-user-d": GeminiClient("gemini-3.5-flash-lite", "key", max_retries=2),
                }
                review_service = GoalReviewService(service=service, router=FakeRouter(clients))
                updated, analysts, review, goal_review, usage = review_service.review_goal(planned.goal_id)

            self.assertEqual(updated.status, "APPROVED")
            self.assertEqual(usage["provider_requests"], 5)
            self.assertEqual(usage["provider_retries"], 1)
            self.assertEqual(goal_review.review_503_count, 0)
            self.assertEqual(goal_review.provider_health["503"], 0)

            metrics = GoalMetricsService(service, runtime_root=root / "runtime").refresh_goal(planned.goal_id)
            self.assertEqual(metrics.providers["503"], 0)
            self.assertEqual(metrics.llm["provider_retries"], 1)

    def test_goal_review_failure_persists_503_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Build JWT login", repo)
            planning = service.update_status(created, "PLANNING", phase="planning")
            planned = service.update_status(planning, "PLANNED", phase="planned")
            store.save_plan_bundle(
                planned,
                SearchPlan(task=planned.goal, search_terms=["jwt"]).to_dict(),
                {"task": planned.goal, "repository": str(repo), "summary": {}, "evidence": [{"path": "server/auth.ts", "line_start": 1, "line_end": 1}]},
                GoalPlan(
                    plan_version=1,
                    goal_id=planned.goal_id,
                    objective=planned.goal,
                    summary="Implement JWT login flow.",
                    tasks=[],
                    candidate_files=["server/auth.ts"],
                    acceptance_criteria=["Login works"],
                    verification=["run tests"],
                    risks=["JWT expiry"],
                    constraints=["No secret leak"],
                    patch_expected=True,
                    uncertainties=[],
                    evidence_refs=["server/auth.ts:1-1"],
                ),
            )

            def fake_urlopen(request, timeout=None):  # noqa: ANN001
                raise _FakeHTTPError(503, '{"error":{"message":"temporarily unavailable"}}')

            with patch("llm.client.urllib.request.urlopen", side_effect=fake_urlopen), patch("llm.client.time.sleep"):
                clients = {
                    "gemini-user-b": GeminiClient("gemini-3.5-flash-lite", "key", max_retries=0),
                    "gemini-user-c": GeminiClient("gemini-3.5-flash-lite", "key", max_retries=0),
                    "gemini-user-a": GeminiClient("gemini-3.5-flash-lite", "key", max_retries=0),
                    "gemini-user-d": GeminiClient("gemini-3.5-flash-lite", "key", max_retries=0),
                }
                review_service = GoalReviewService(service=service, router=FakeRouter(clients))
                with self.assertRaises(LLMError) as ctx:
                    review_service.review_goal(planned.goal_id)

            self.assertEqual(ctx.exception.kind, "SERVICE_UNAVAILABLE")
            review_record = store.load(planned.goal_id)
            saved_review = json.loads((store.goal_dir(planned.goal_id) / "review.json").read_text(encoding="utf-8"))
            self.assertEqual(review_record.status, "REVIEW_FAILED")
            self.assertEqual(saved_review["review_503_count"], 1)
            self.assertEqual(saved_review["provider_health"]["503"], 1)

            metrics_service = GoalMetricsService(service, runtime_root=root / "runtime")
            metrics = metrics_service.refresh_goal(planned.goal_id)
            self.assertEqual(metrics.providers["503"], 1)
            self.assertGreaterEqual(metrics.llm["provider_retries"], 0)

    def test_phase3_prompt_is_pre_implementation_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            store = GoalStore(root / "runtime" / "goals")
            service = GoalService(store)
            created = service.create_goal("Add type hints to calculator.py", repo)
            planning = service.update_status(created, "PLANNING", phase="planning")
            planned = service.update_status(planning, "PLANNED", phase="planned")
            search_plan = SearchPlan(task=planned.goal, search_terms=["calculator.py", "add"])
            evidence = {
                "task": planned.goal,
                "repository": str(repo),
                "summary": {"files_inspected": 1, "lines_captured": 1},
                "evidence": [{"path": "calculator.py", "line_start": 1, "line_end": 2}],
            }
            plan = GoalPlan(
                plan_version=1,
                goal_id=planned.goal_id,
                objective=planned.goal,
                summary="Propose type hints for add.",
                tasks=[],
                candidate_files=["calculator.py"],
                acceptance_criteria=["Type hints added"],
                verification=["pytest"],
                risks=["Typing mismatch"],
                constraints=["No secret leak"],
                patch_expected=True,
                uncertainties=[],
                evidence_refs=["calculator.py:1-2"],
            )
            store.save_plan_bundle(planned, search_plan.to_dict(), evidence, plan)

            analyst_client = CapturingClient(_verdict_payload("analyst-1", "gemini-user-b", "PASS"))
            reviewer_client = CapturingClient(
                {
                    "final_verdict": "PASS",
                    "confidence": 0.9,
                    "agreement": "FULL",
                    "reason": "Approved.",
                    "analyst_a": {"verdict": "PASS"},
                    "analyst_b": {"verdict": "PASS"},
                    "analysts": [{"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "PASS"}],
                    "evidence": [{"path": "calculator.py", "lines": "1-2"}],
                    "patch_required": False,
                }
            )
            clients = {
                "gemini-user-b": analyst_client,
                "gemini-user-c": CapturingClient(_verdict_payload("analyst-2", "gemini-user-c", "PASS")),
                "gemini-user-a": CapturingClient(_verdict_payload("analyst-3", "gemini-user-a", "PASS")),
                "gemini-user-d": reviewer_client,
            }
            review_service = GoalReviewService(service=service, router=FakeRouter(clients))
            updated, analysts, review, goal_review, usage = review_service.review_goal(planned.goal_id)

            self.assertEqual(updated.status, "APPROVED")
            self.assertIn("before any code has been changed", analyst_client.system_prompts[0])
            self.assertIn("before any code has been changed", reviewer_client.system_prompts[0])
            self.assertIn("pre-implementation", analyst_client.system_prompts[0].lower())
            self.assertIn("pre-implementation", reviewer_client.system_prompts[0].lower())
            self.assertEqual(review.patch_required, False)
            self.assertEqual(goal_review.status, "APPROVED")


def _verdict_payload(analyst: str, profile: str, verdict: str) -> dict:
    return {
        "analyst": analyst,
        "profile": profile,
        "verdict": verdict,
        "confidence": 0.8,
        "reason": "Supported by evidence.",
        "evidence": [{"path": "server/auth.ts", "lines": "1-1"}],
        "uncertainties": [],
    }


def _analyst_text(counter: int) -> str:
    return json.dumps(
        {
            "analyst": f"analyst-{counter}",
            "profile": f"profile-{counter}",
            "verdict": "PASS",
            "confidence": 0.9,
            "reason": "Supported.",
            "evidence": [{"path": "server/auth.ts", "lines": "1-1"}],
            "uncertainties": [],
        },
        ensure_ascii=False,
    )


def _review_text() -> str:
    return json.dumps(
        {
            "final_verdict": "PASS",
            "confidence": 0.9,
            "agreement": "FULL",
            "reason": "Approved.",
            "analyst_a": {"verdict": "PASS"},
            "analyst_b": {"verdict": "PASS"},
            "analysts": [{"verdict": "PASS"}, {"verdict": "PASS"}, {"verdict": "PASS"}],
            "evidence": [{"path": "server/auth.ts", "lines": "1-1"}],
            "patch_required": False,
        },
        ensure_ascii=False,
    )


if __name__ == "__main__":
    unittest.main()
