from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from goal.audit_report import is_read_only_audit
from goal.service import GoalService
from goal.store import GoalStore


class AuditIntentClassificationTests(unittest.TestCase):
    def test_scoped_do_not_modify_is_still_an_edit_task(self) -> None:
        goal = (
            "Fix the invoice discount calculation so all tests pass. "
            "Only modify src/invoice.py. Do not modify formatting code, tests, "
            "configuration, or create new project files."
        )
        self.assertFalse(is_read_only_audit(goal))

    def test_turkish_scoped_restriction_is_still_an_edit_task(self) -> None:
        goal = "Hesaplama hatasını düzelt. Yalnızca src/invoice.py dosyasını değiştir; testleri değiştirme."
        self.assertFalse(is_read_only_audit(goal))

    def test_explicit_read_only_language_remains_fail_closed(self) -> None:
        self.assertTrue(is_read_only_audit("Explain architecture and find bugs, do not modify files."))
        self.assertTrue(is_read_only_audit("Review possible fixes without modifying any files."))
        self.assertTrue(is_read_only_audit("Bu projeyi hiçbir dosyayı değiştirmeden incele ve raporla."))

    def test_plan_explicitly_marked_no_patch_remains_read_only(self) -> None:
        self.assertTrue(is_read_only_audit("Fix the bug", {"patch_expected": False}))

    def test_goal_service_classifies_scoped_fix_as_code_modification(self) -> None:
        goal = (
            "Fix the invoice discount calculation so all tests pass. "
            "Only modify src/invoice.py. Do not modify formatting code, tests, "
            "configuration, or create new project files."
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            service = GoalService(GoalStore(root / "goals"))
            record = service.create_goal(goal, repo)
        self.assertEqual(record.goal_type, "CODE_MODIFICATION")


if __name__ == "__main__":
    unittest.main()
