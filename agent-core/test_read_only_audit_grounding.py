from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from goal.audit_report import generate_audit_report
from goal.audit_validator import GroundedFinding, GroundedFindingValidator
from goal.audit_synthesizer import AuditSynthesizer
from schemas.verdict import Verdict
from schemas.review import Review


class AuditGroundingAndAntiCheatingTests(unittest.TestCase):
    def test_calculator_repo_never_contains_trading_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "calculator_repo"
            repo.mkdir()
            (repo / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n"
                "    return a + b\n\n"
                "def divide(a: int, b: int) -> float:\n"
                "    return a / b\n",
                encoding="utf-8",
            )
            (repo / "requirements.txt").write_text("pytest>=7.0.0\n", encoding="utf-8")

            evidence_packet = {
                "repo_map": {
                    "entry_points": ["calc.py"],
                    "configs": [],
                    "manifests": ["requirements.txt"],
                    "top_packages": ["root"],
                    "tests": [],
                },
                "evidence": [
                    {
                        "path": "calc.py",
                        "line_start": 1,
                        "line_end": 6,
                        "content": (
                            "def add(a: int, b: int) -> int:\n"
                            "    return a + b\n\n"
                            "def divide(a: int, b: int) -> float:\n"
                            "    return a / b\n"
                        ),
                    }
                ],
            }

            analysts = [
                Verdict(
                    verdict="PASS",
                    confidence=0.9,
                    reason="Calculator library with basic arithmetic operations.",
                    evidence=[{"path": "calc.py", "lines": "1-6"}],
                )
            ]
            review = Review(
                final_verdict="PASS",
                confidence=0.9,
                agreement="FULL",
                reason="Simple math operations verified.",
                patch_required=False,
                analysts=[a.to_dict() for a in analysts],
                analyst_a=analysts[0].to_dict(),
                analyst_b=analysts[0].to_dict(),
                evidence=[{"path": "calc.py", "lines": "1-6"}],
            )

            report = generate_audit_report(
                goal_id="GOAL-CALC-0001",
                goal_text="Perform audit of calculator",
                repo_path=repo,
                plan_dict={"patch_expected": False},
                evidence_packet=evidence_packet,
                analyst_results=analysts,
                review_dict=review.to_dict(),
            )

            # Assert NO trading / finance terms leaked into calculator report
            self.assertNotIn("Backtrader", report)
            self.assertNotIn("S&P", report)
            self.assertNotIn("S&P 500", report)
            self.assertNotIn("slippage", report.lower())
            self.assertNotIn("look-ahead bias", report.lower())
            self.assertNotIn("survivorship bias", report.lower())
            self.assertNotIn("backtest", report.lower())
            self.assertNotIn("alpaca", report.lower())
            self.assertIn("calc.py", report)

    def test_web_repo_never_receives_trading_findings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "web_repo"
            repo.mkdir()
            (repo / "app.py").write_text(
                "from flask import Flask, jsonify, request\n"
                "app = Flask(__name__)\n\n"
                "@app.route('/health')\n"
                "def health():\n"
                "    return jsonify({'status': 'ok'})\n",
                encoding="utf-8",
            )
            (repo / "config.json").write_text('{"port": 8080, "debug": false}', encoding="utf-8")

            evidence_packet = {
                "repo_map": {
                    "entry_points": ["app.py"],
                    "configs": ["config.json"],
                    "manifests": [],
                    "top_packages": ["root"],
                    "tests": [],
                },
                "evidence": [
                    {
                        "path": "app.py",
                        "line_start": 1,
                        "line_end": 7,
                        "content": (
                            "from flask import Flask, jsonify, request\n"
                            "app = Flask(__name__)\n\n"
                            "@app.route('/health')\n"
                            "def health():\n"
                            "    return jsonify({'status': 'ok'})\n"
                        ),
                    }
                ],
            }

            analysts = [
                Verdict(
                    verdict="PASS",
                    confidence=0.88,
                    reason="Web service with REST API endpoints.",
                    evidence=[{"path": "app.py", "lines": "1-7"}],
                )
            ]
            review = Review(
                final_verdict="PASS",
                confidence=0.88,
                agreement="FULL",
                reason="Flask HTTP endpoint verified.",
                patch_required=False,
                analysts=[a.to_dict() for a in analysts],
                analyst_a=analysts[0].to_dict(),
                analyst_b=analysts[0].to_dict(),
                evidence=[{"path": "app.py", "lines": "1-7"}],
            )

            report = generate_audit_report(
                goal_id="GOAL-WEB-0001",
                goal_text="Perform audit of web service",
                repo_path=repo,
                plan_dict={"patch_expected": False},
                evidence_packet=evidence_packet,
                analyst_results=analysts,
                review_dict=review.to_dict(),
            )

            self.assertNotIn("Backtrader", report)
            self.assertNotIn("slippage", report.lower())
            self.assertNotIn("order matching", report.lower())
            self.assertIn("app.py", report)
            self.assertIn("config.json", report)

    def test_unrelated_content_with_engine_filename_triggers_no_trading_findings(self) -> None:
        """A file named engine/backtest_engine.py with cooking recipe contents must NOT produce trading findings."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "fake_trading_repo"
            repo.mkdir()
            engine_dir = repo / "engine"
            engine_dir.mkdir()
            (engine_dir / "backtest_engine.py").write_text(
                "# Cookie Recipe\n"
                "def bake_cookies(cups_of_flour: int, cups_of_sugar: int) -> str:\n"
                "    return f'Baked {cups_of_flour + cups_of_sugar} delicious cookies!'\n",
                encoding="utf-8",
            )

            evidence_packet = {
                "repo_map": {
                    "entry_points": ["engine/backtest_engine.py"],
                    "configs": [],
                    "manifests": [],
                    "top_packages": ["engine"],
                    "tests": [],
                },
                "evidence": [
                    {
                        "path": "engine/backtest_engine.py",
                        "line_start": 1,
                        "line_end": 4,
                        "content": (
                            "# Cookie Recipe\n"
                            "def bake_cookies(cups_of_flour: int, cups_of_sugar: int) -> str:\n"
                            "    return f'Baked {cups_of_flour + cups_of_sugar} delicious cookies!'\n"
                        ),
                    }
                ],
            }

            analysts = [
                Verdict(
                    verdict="PASS",
                    confidence=0.85,
                    reason="Cookie recipe module.",
                    evidence=[{"path": "engine/backtest_engine.py", "lines": "1-4"}],
                )
            ]
            review = Review(
                final_verdict="PASS",
                confidence=0.85,
                agreement="FULL",
                reason="Baking logic verified.",
                patch_required=False,
                analysts=[a.to_dict() for a in analysts],
                analyst_a=analysts[0].to_dict(),
                analyst_b=analysts[0].to_dict(),
                evidence=[{"path": "engine/backtest_engine.py", "lines": "1-4"}],
            )

            report = generate_audit_report(
                goal_id="GOAL-BAKE-0001",
                goal_text="Audit recipe module",
                repo_path=repo,
                plan_dict={"patch_expected": False},
                evidence_packet=evidence_packet,
                analyst_results=analysts,
                review_dict=review.to_dict(),
            )

            # Must NOT claim Cerebro, cheat_on_open, slippage, commission, or S&P 500!
            self.assertNotIn("Cerebro", report)
            self.assertNotIn("cheat_on_open", report)
            self.assertNotIn("slippage", report.lower())
            self.assertNotIn("commission", report.lower())
            self.assertNotIn("PaperRiskConfig", report)
            self.assertIn("engine/backtest_engine.py", report)

    def test_nonexistent_files_are_dropped_or_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "empty_repo"
            repo.mkdir()

            validator = GroundedFindingValidator(repo)
            raw = [
                {
                    "title": "Critical Bug in Phantom File",
                    "severity": "CRITICAL",
                    "file": "does_not_exist.py",
                    "symbol": "phantom",
                    "start_line": 10,
                    "end_line": 20,
                    "claim": "Catastrophic memory leak.",
                    "evidence_excerpt": "leak()",
                    "confidence": 0.99,
                    "status": "VERIFIED",
                }
            ]

            validated = validator.validate(raw)
            # Must drop ungrounded CRITICAL claims referencing non-existent files
            self.assertEqual(len(validated), 0)

    def test_moving_code_lines_produces_updated_citations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "shifted_repo"
            repo.mkdir()
            # Insert 50 blank/comment lines before the target function
            padding = "\n".join(f"# comment line {i}" for i in range(50))
            (repo / "service.py").write_text(
                f"{padding}\n"
                f"def calculate_total(items: list) -> float:\n"
                f"    return sum(item.price for item in items)\n",
                encoding="utf-8",
            )

            validator = GroundedFindingValidator(repo)
            raw = [
                {
                    "title": "Calculate Total Function",
                    "severity": "OBSERVATION",
                    "file": "service.py",
                    "symbol": "calculate_total",
                    "start_line": 1,  # Stale line number
                    "end_line": 3,
                    "claim": "Calculates total price.",
                    "evidence_excerpt": "def calculate_total(items: list) -> float:",
                    "confidence": 0.9,
                    "status": "VERIFIED",
                }
            ]

            validated = validator.validate(raw)
            self.assertEqual(len(validated), 1)
            # Validator must locate the real line on disk (around line 51)
            self.assertGreaterEqual(validated[0].start_line, 50)
            self.assertIn("def calculate_total", validated[0].evidence_excerpt)


if __name__ == "__main__":
    unittest.main()
