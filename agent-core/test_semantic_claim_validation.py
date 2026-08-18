from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evidence.builder import EvidenceBuilder
from goal.audit_report import generate_audit_report
from goal.audit_synthesizer import AuditSynthesizer
from goal.audit_validator import (
    FindingCandidate,
    SemanticClaimValidator,
    SourceObservation,
    UnconfirmedRisk,
    ValidatedFinding,
)
from schemas.review import Review
from schemas.verdict import Verdict


class SemanticClaimValidationTests(unittest.TestCase):
    def test_correct_code_produces_zero_verified_defects(self) -> None:
        """Fixtures with correct arithmetic, dataclasses, CLI args, prints, and offline shift targets must produce ZERO verified defects."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "clean_repo"
            repo.mkdir()
            (repo / "clean_module.py").write_text(
                "from dataclasses import dataclass\n"
                "import pandas as pd\n\n"
                "@dataclass\n"
                "class Config:\n"
                "    threshold: float = 0.05\n\n"
                "def evaluate_strategy(df: pd.DataFrame) -> dict:\n"
                "    # Offline target calculation\n"
                "    df['next_return'] = df['close'].shift(-1) / df['close'] - 1.0\n"
                "    mean_ret = df['next_return'].mean()\n"
                "    print(f'Average forward return: {mean_ret}')\n"
                "    # Drawdown calculation with cumulative max\n"
                "    equity = df['close']\n"
                "    drawdown = equity / equity.cummax() - 1.0\n"
                "    return {'mean_ret': mean_ret, 'max_dd': drawdown.min()}\n",
                encoding="utf-8",
            )

            validator = SemanticClaimValidator(repo)
            raw_candidates = [
                {
                    "title": "Dataclass Observation",
                    "severity": "OBSERVATION",
                    "file": "clean_module.py",
                    "symbol": "Config",
                    "start_line": 5,
                    "end_line": 7,
                    "claim": "Defines class Config in clean_module.py.",
                    "evidence_excerpt": "class Config:\n    threshold: float = 0.05",
                },
                {
                    "title": "Future Shift Operation",
                    "severity": "CRITICAL",
                    "file": "clean_module.py",
                    "symbol": "",
                    "start_line": 10,
                    "end_line": 10,
                    "claim": "Calls .shift(-1)",
                    "problem": "Negative shift loads future prices.",
                    "evidence_excerpt": "df['next_return'] = df['close'].shift(-1) / df['close'] - 1.0",
                },
                {
                    "title": "Potential Division By Zero",
                    "severity": "HIGH",
                    "file": "clean_module.py",
                    "symbol": "",
                    "start_line": 14,
                    "end_line": 14,
                    "claim": "Division by cummax",
                    "problem": "Drawdown calculation divides by equity.cummax()",
                    "evidence_excerpt": "drawdown = equity / equity.cummax() - 1.0",
                },
            ]

            verified, risks, observations = validator.process(raw_candidates)

            # Must produce ZERO verified defects
            self.assertEqual(len(verified), 0)
            self.assertGreater(len(observations), 0)
            self.assertTrue(any("Offline Future Target" in r.title for r in risks))

    def test_pandas_series_division_is_not_zero_division_error(self) -> None:
        """Pandas Series division (frame['spy_close'] / frame['spy_peak']) must NOT be classified as ZeroDivisionError defect."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "pandas_repo"
            repo.mkdir()
            (repo / "metrics.py").write_text(
                "import pandas as pd\n\n"
                "def calculate_drawdown(frame: pd.DataFrame) -> pd.DataFrame:\n"
                "    frame['spy_peak'] = frame['spy_close'].cummax()\n"
                "    frame['spy_drawdown'] = frame['spy_close'] / frame['spy_peak'] - 1.0\n"
                "    return frame\n",
                encoding="utf-8",
            )

            validator = SemanticClaimValidator(repo)
            raw = [
                {
                    "title": "Potential Division By Zero",
                    "severity": "HIGH",
                    "file": "metrics.py",
                    "symbol": "calculate_drawdown",
                    "start_line": 5,
                    "end_line": 5,
                    "claim": "Division by frame['spy_peak']",
                    "problem": "Dynamic division without zero-value check.",
                    "evidence_excerpt": "frame['spy_drawdown'] = frame['spy_close'] / frame['spy_peak'] - 1.0",
                }
            ]

            verified, risks, observations = validator.process(raw)
            self.assertEqual(len(verified), 0)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].category, "CALCULATION")
            self.assertIn("Vectorized Pandas/NumPy", observations[0].description)

    def test_numpy_array_division_is_not_zero_division_error(self) -> None:
        """NumPy array division (np_arr1 / np_arr2) must NOT be classified as ZeroDivisionError defect."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "np_repo"
            repo.mkdir()
            (repo / "norm.py").write_text(
                "import numpy as np\n\n"
                "def normalize(arr_a: np.ndarray, arr_b: np.ndarray) -> np.ndarray:\n"
                "    return np.divide(arr_a, arr_b)\n",
                encoding="utf-8",
            )

            validator = SemanticClaimValidator(repo)
            raw = [
                {
                    "title": "Potential Division By Zero",
                    "severity": "HIGH",
                    "file": "norm.py",
                    "symbol": "normalize",
                    "start_line": 4,
                    "end_line": 4,
                    "claim": "Division operation",
                    "problem": "Array division",
                    "evidence_excerpt": "return np.divide(arr_a, arr_b)",
                }
            ]

            verified, risks, observations = validator.process(raw)
            self.assertEqual(len(verified), 0)

    def test_guarded_scalar_division_is_safe_observation(self) -> None:
        """Guarded division (if count != 0: return total / count) must NOT be classified as defect."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "guarded_repo"
            repo.mkdir()
            (repo / "calc.py").write_text(
                "def average(total: float, count: int) -> float:\n"
                "    if count != 0:\n"
                "        return total / count\n"
                "    return 0.0\n",
                encoding="utf-8",
            )

            validator = SemanticClaimValidator(repo)
            raw = [
                {
                    "title": "Potential Division By Zero",
                    "severity": "HIGH",
                    "file": "calc.py",
                    "symbol": "average",
                    "start_line": 3,
                    "end_line": 3,
                    "claim": "Division by count",
                    "problem": "Divides by count",
                    "evidence_excerpt": "return total / count",
                }
            ]

            verified, risks, observations = validator.process(raw)
            self.assertEqual(len(verified), 0)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].category, "CALCULATION")

    def test_constant_denominator_is_safe_observation(self) -> None:
        """Arithmetic with non-zero constant denominator (/ 1000) must be classified as safe calculation."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "fee_repo"
            repo.mkdir()
            (repo / "fee.py").write_text(
                "def compute_fee(cents: int) -> int:\n"
                "    return int(cents * 15 / 1000)\n",
                encoding="utf-8",
            )

            validator = SemanticClaimValidator(repo)
            raw = [
                {
                    "title": "Potential Division By Zero",
                    "severity": "HIGH",
                    "file": "fee.py",
                    "symbol": "compute_fee",
                    "start_line": 2,
                    "end_line": 2,
                    "claim": "Divides by 1000",
                    "problem": "Division operation",
                    "evidence_excerpt": "return int(cents * 15 / 1000)",
                }
            ]

            verified, risks, observations = validator.process(raw)
            self.assertEqual(len(verified), 0)
            self.assertEqual(len(observations), 1)
            self.assertIn("constant `1000`", observations[0].description)

    def test_mutation_from_pandas_to_scalar_changes_classification(self) -> None:
        """Proves that mutating Pandas Series division to Python scalar division flips classification to ValidatedFinding."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "mutation_repo"
            repo.mkdir()

            # Version A: Pandas Series division
            (repo / "calc.py").write_text(
                "import pandas as pd\n"
                "def compute(df: pd.DataFrame):\n"
                "    df['ratio'] = df['close'] / df['volume']\n",
                encoding="utf-8",
            )
            validator = SemanticClaimValidator(repo)
            raw_a = [{
                "title": "Division Operation",
                "severity": "HIGH",
                "file": "calc.py",
                "symbol": "compute",
                "start_line": 3,
                "end_line": 3,
                "claim": "df['close'] / df['volume']",
                "problem": "Dynamic division",
                "evidence_excerpt": "df['ratio'] = df['close'] / df['volume']",
            }]
            verified_a, _, obs_a = validator.process(raw_a)
            self.assertEqual(len(verified_a), 0)
            self.assertEqual(len(obs_a), 1)

            # Version B: Mutated to pure Python scalar division
            (repo / "calc.py").write_text(
                "def compute(close: float, volume: float):\n"
                "    ratio = close / volume\n",
                encoding="utf-8",
            )
            raw_b = [{
                "title": "Division Operation",
                "severity": "HIGH",
                "file": "calc.py",
                "symbol": "compute",
                "start_line": 2,
                "end_line": 2,
                "claim": "close / volume",
                "problem": "Scalar dynamic division without zero check",
                "evidence_excerpt": "ratio = close / volume",
            }]
            verified_b, _, obs_b = validator.process(raw_b)
            self.assertEqual(len(verified_b), 1)
            self.assertEqual(verified_b[0].severity, "HIGH")
            self.assertIn("Scalar Division By Zero", verified_b[0].title)

    def test_readme_line_never_receives_code_analysis_description(self) -> None:
        """Observations from README files must have clean documentation text, never code defect claims."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "doc_repo"
            repo.mkdir()
            (repo / "README.md").write_text("# Project Info\nSupports v1.31 forex/stock trading.\n", encoding="utf-8")

            validator = SemanticClaimValidator(repo)
            raw = [{
                "title": "Division Operation",
                "severity": "HIGH",
                "file": "README.md",
                "symbol": "",
                "start_line": 2,
                "end_line": 2,
                "claim": "v1.31 forex/stock trading",
                "problem": "Dynamic division without verified zero-value validation.",
                "evidence_excerpt": "Supports v1.31 forex/stock trading.",
            }]

            verified, risks, observations = validator.process(raw)
            self.assertEqual(len(verified), 0)
            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].category, "DOCUMENTATION")
            self.assertNotIn("division", observations[0].description.lower())
            self.assertIn("Supports v1.31", observations[0].description)

    def test_future_leakage_into_trading_decision_is_verified_critical(self) -> None:
        """When future target is consumed by trade execution logic (if next_return > 0: buy()), it MUST be verified as CRITICAL."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "leaking_repo"
            repo.mkdir()
            (repo / "leaking_strategy.py").write_text(
                "import pandas as pd\n\n"
                "def execute_trades(df: pd.DataFrame) -> None:\n"
                "    df['next_return'] = df['close'].shift(-1) / df['close'] - 1.0\n"
                "    for idx, row in df.iterrows():\n"
                "        if row['next_return'] > 0:\n"
                "            buy(idx, row['close'])\n",
                encoding="utf-8",
            )

            validator = SemanticClaimValidator(repo)
            raw = [
                {
                    "title": "Future Data Shift Operation",
                    "severity": "CRITICAL",
                    "file": "leaking_strategy.py",
                    "symbol": "",
                    "start_line": 4,
                    "end_line": 4,
                    "claim": "Calls .shift(-1)",
                    "problem": "Shift operation loads future prices.",
                    "evidence_excerpt": "df['next_return'] = df['close'].shift(-1) / df['close'] - 1.0",
                }
            ]

            verified, risks, observations = validator.process(raw)
            self.assertEqual(len(verified), 1)
            self.assertEqual(verified[0].severity, "CRITICAL")
            self.assertIn("Look-Ahead Bias", verified[0].title)
            self.assertIn("if row['next_return'] > 0:", verified[0].mechanism)

    def test_unguarded_division_by_zero_is_verified_high(self) -> None:
        """Dynamic division without zero check on variable denominator must be verified as HIGH."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "div_repo"
            repo.mkdir()
            (repo / "calc.py").write_text(
                "def compute_ratio(total_volume: float, count: int) -> float:\n"
                "    return total_volume / count\n",
                encoding="utf-8",
            )

            validator = SemanticClaimValidator(repo)
            raw = [
                {
                    "title": "Potential Division By Zero",
                    "severity": "HIGH",
                    "file": "calc.py",
                    "symbol": "compute_ratio",
                    "start_line": 2,
                    "end_line": 2,
                    "claim": "Divides by count",
                    "problem": "Division without count != 0 guard.",
                    "evidence_excerpt": "return total_volume / count",
                }
            ]

            verified, risks, observations = validator.process(raw)
            self.assertEqual(len(verified), 1)
            self.assertEqual(verified[0].severity, "HIGH")
            self.assertIn("Scalar Division By Zero", verified[0].title)

    def test_backtest_engine_not_classified_as_test_file(self) -> None:
        """Verify that engine/backtest_engine.py is NOT classified as a test file by EvidenceBuilder."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            engine_dir = repo / "engine"
            engine_dir.mkdir()
            (engine_dir / "backtest_engine.py").write_text("class BacktestEngine:\n    pass\n", encoding="utf-8")
            tests_dir = repo / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_engine.py").write_text("def test_engine():\n    pass\n", encoding="utf-8")

            builder = EvidenceBuilder(repo, {"task": "audit"})
            repo_map = builder._build_repo_map()

            self.assertNotIn("engine/backtest_engine.py", repo_map["tests"])
            self.assertIn("tests/test_engine.py", repo_map["tests"])

    def test_empty_verified_findings_outputs_explicit_no_defect_message(self) -> None:
        """When no defects are proven, the report must state: 'No source-backed defect was proven in the inspected scope.'"""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "clean_app"
            repo.mkdir()
            (repo / "main.py").write_text("print('Hello World')\n", encoding="utf-8")

            evidence_packet = {
                "repo_map": {
                    "entry_points": ["main.py"],
                    "configs": [],
                    "manifests": [],
                    "top_packages": ["root"],
                    "tests": [],
                },
                "evidence": [
                    {
                        "path": "main.py",
                        "line_start": 1,
                        "line_end": 1,
                        "content": "print('Hello World')",
                    }
                ],
            }

            analysts = [
                Verdict(
                    verdict="PASS",
                    confidence=0.9,
                    reason="Clean application without defects.",
                    evidence=[{"path": "main.py", "lines": "1-1"}],
                )
            ]
            review = Review(
                final_verdict="PASS",
                confidence=0.9,
                agreement="FULL",
                reason="Clean codebase.",
                patch_required=False,
                analysts=[a.to_dict() for a in analysts],
                analyst_a=analysts[0].to_dict(),
                analyst_b=analysts[0].to_dict(),
                evidence=[{"path": "main.py", "lines": "1-1"}],
            )

            report = generate_audit_report(
                goal_id="GOAL-CLEAN-0001",
                goal_text="Audit clean app",
                repo_path=repo,
                plan_dict={"patch_expected": False},
                evidence_packet=evidence_packet,
                analyst_results=analysts,
                review_dict=review.to_dict(),
            )

            self.assertIn("No source-backed defect was proven in the inspected scope.", report)


if __name__ == "__main__":
    unittest.main()
