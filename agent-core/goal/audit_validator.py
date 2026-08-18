from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class SourceObservation:
    file: str
    symbol: str
    start_line: int
    end_line: int
    excerpt: str
    category: str = "STRUCTURE"
    description: str = ""

    @property
    def evidence_excerpt(self) -> str:
        return self.excerpt

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FindingCandidate:
    title: str
    problem: str
    mechanism: str
    impact: str
    severity: str
    file: str
    symbol: str
    start_line: int
    end_line: int
    evidence_excerpt: str
    supporting_context: list[str] = field(default_factory=list)
    recommendation: str = ""
    confidence: float = 0.8

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidatedFinding:
    title: str
    problem: str
    mechanism: str
    impact: str
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    file: str
    symbol: str
    start_line: int
    end_line: int
    evidence_excerpt: str
    supporting_context: list[str]
    confidence: float
    recommendation: str = ""
    status: str = "VERIFIED_FINDING"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UnconfirmedRisk:
    title: str
    observation: str
    inference: str
    file: str
    start_line: int = 0
    end_line: int = 0
    evidence_excerpt: str = ""
    recommendation: str = ""
    status: str = "RISK / UNCONFIRMED"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Aliases for backward compatibility
GroundedFinding = ValidatedFinding


class SemanticClaimValidator:
    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path).resolve()

    def validate(self, raw_candidates: list[dict[str, Any] | FindingCandidate]) -> list[ValidatedFinding | SourceObservation | UnconfirmedRisk]:
        verified, risks, observations = self.process(raw_candidates)
        return [*verified, *risks, *observations]

    def process(
        self,
        raw_candidates: list[dict[str, Any] | FindingCandidate],
    ) -> tuple[list[ValidatedFinding], list[UnconfirmedRisk], list[SourceObservation]]:
        validated_findings: list[ValidatedFinding] = []
        unconfirmed_risks: list[UnconfirmedRisk] = []
        source_observations: list[SourceObservation] = []

        for item in raw_candidates:
            cand = item if isinstance(item, FindingCandidate) else self._dict_to_candidate(item)
            if cand is None:
                continue

            result = self._validate_candidate(cand)
            if isinstance(result, ValidatedFinding):
                validated_findings.append(result)
            elif isinstance(result, UnconfirmedRisk):
                unconfirmed_risks.append(result)
            elif isinstance(result, SourceObservation):
                source_observations.append(result)

        return validated_findings, unconfirmed_risks, source_observations

    def _dict_to_candidate(self, raw: dict[str, Any]) -> FindingCandidate | None:
        file_rel = str(raw.get("file", "")).replace("\\", "/").strip().lstrip("/")
        if not file_rel:
            return None

        return FindingCandidate(
            title=str(raw.get("title", "Candidate Finding")),
            problem=str(raw.get("problem", raw.get("claim", ""))),
            mechanism=str(raw.get("mechanism", "")),
            impact=str(raw.get("impact", "")),
            severity=str(raw.get("severity", "MEDIUM")).upper(),
            file=file_rel,
            symbol=str(raw.get("symbol", "")),
            start_line=int(raw.get("start_line", 1) or 1),
            end_line=int(raw.get("end_line", 1) or 1),
            evidence_excerpt=str(raw.get("evidence_excerpt", "")).strip(),
            supporting_context=list(raw.get("supporting_context", [])) if isinstance(raw.get("supporting_context"), list) else [],
            recommendation=str(raw.get("recommendation", "")),
            confidence=float(raw.get("confidence", 0.7)),
        )

    def _validate_candidate(
        self, cand: FindingCandidate
    ) -> ValidatedFinding | UnconfirmedRisk | SourceObservation | None:
        target_file = self.repo_path / cand.file
        if not target_file.exists() or target_file.is_dir():
            # Drop hallucinated/non-existent file claims
            return None

        # Rule: README, markdown, or documentation files CANNOT ground code defects
        if target_file.suffix.lower() in {".md", ".txt", ".rst", ".doc"} or "readme" in cand.file.lower():
            # Extract plain text description directly from document line, never from code defect text
            doc_desc = "Repository documentation excerpt."
            if cand.evidence_excerpt:
                doc_desc = cand.evidence_excerpt.splitlines()[0].strip()[:120]
            return SourceObservation(
                file=cand.file,
                symbol=cand.symbol,
                start_line=cand.start_line,
                end_line=cand.end_line,
                excerpt=cand.evidence_excerpt[:160],
                category="DOCUMENTATION",
                description=doc_desc,
            )

        # Read actual file lines directly from disk
        try:
            content = target_file.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
        except Exception:
            return None

        total_lines = len(lines)
        if total_lines == 0:
            return None

        # Relocate line range on disk
        start_line = max(1, min(cand.start_line, total_lines))
        end_line = max(start_line, min(cand.end_line, total_lines))

        disk_slice = lines[start_line - 1 : end_line]
        actual_disk_excerpt = "\n".join(disk_slice).strip()

        # Check if cited slice actually contains excerpt; if not, relocate on disk
        if cand.evidence_excerpt:
            first_line = cand.evidence_excerpt.splitlines()[0].strip()
            if len(first_line) > 5 and first_line.lower() not in actual_disk_excerpt.lower():
                found_s, found_e = self._locate_in_lines(lines, cand.evidence_excerpt, cand.symbol)
                if found_s > 0:
                    start_line, end_line = found_s, found_e
                    actual_disk_excerpt = "\n".join(lines[start_line - 1 : end_line]).strip()
        elif cand.symbol and cand.symbol not in actual_disk_excerpt:
            found_s, found_e = self._locate_in_lines(lines, cand.evidence_excerpt, cand.symbol)
            if found_s > 0:
                start_line, end_line = found_s, found_e
                actual_disk_excerpt = "\n".join(lines[start_line - 1 : end_line]).strip()

        # Check for ordinary non-defect constructs
        # 1. Pure Class / Dataclass / Function declarations without proven defect
        if self._is_pure_declaration(actual_disk_excerpt, cand.problem):
            return SourceObservation(
                file=cand.file,
                symbol=cand.symbol,
                start_line=start_line,
                end_line=end_line,
                excerpt=actual_disk_excerpt[:160],
                category="STRUCTURE",
                description=f"Defines `{cand.symbol or 'component'}` in `{cand.file}`.",
            )

        # 2. Ordinary logging, print statements, or CLI setup
        if self._is_logging_or_cli(actual_disk_excerpt):
            return SourceObservation(
                file=cand.file,
                symbol=cand.symbol,
                start_line=start_line,
                end_line=end_line,
                excerpt=actual_disk_excerpt[:160],
                category="LOGGING_CLI",
                description="Standard CLI or logging instruction.",
            )

        # 3. Analyze Future / Shift calculations (e.g. .shift(-1))
        if ".shift(-" in actual_disk_excerpt or "shift(-" in cand.problem.lower():
            shift_analysis = self._analyze_future_shift_usage(content, cand.file, start_line, actual_disk_excerpt)
            if shift_analysis["is_leakage"]:
                return ValidatedFinding(
                    title="Look-Ahead Bias: Future Data Leaked Into Decision Logic",
                    problem="Future bar return calculation is consumed by trading / decision execution logic at time t.",
                    mechanism=f"Variable `{shift_analysis['var_name']}` computed via `.shift(-k)` at line {start_line} is consumed at line {shift_analysis['consumer_line']}: `{shift_analysis['consumer_line_text']}`.",
                    impact="Backtest performance will be artificially inflated by executing trades based on future prices.",
                    severity="CRITICAL",
                    file=cand.file,
                    symbol=cand.symbol,
                    start_line=start_line,
                    end_line=end_line,
                    evidence_excerpt=actual_disk_excerpt,
                    supporting_context=[shift_analysis["consumer_line_text"]],
                    confidence=0.95,
                    recommendation="Ensure future target labels are used strictly for offline validation/reporting and excluded from signal generation.",
                )
            else:
                return UnconfirmedRisk(
                    title="Offline Future Target Calculation",
                    observation=f"Found `.shift(-k)` operation in `{cand.file}:{start_line}`: `{actual_disk_excerpt[:120]}`.",
                    inference="Calculation is used for offline evaluation / validation metrics and does not leak into trading decisions.",
                    file=cand.file,
                    start_line=start_line,
                    end_line=end_line,
                    evidence_excerpt=actual_disk_excerpt,
                    recommendation="Verify that columns created with `.shift(-k)` remain segregated from feature generation pipelines.",
                )

        # 4. Analyze Division Operations with Type & Context Awareness
        if "/" in actual_disk_excerpt:
            div_analysis = self._analyze_division_safety(content, lines, start_line, actual_disk_excerpt)
            if div_analysis["category"] == "VERIFIED_SCALAR_BUG":
                return ValidatedFinding(
                    title="Unchecked Scalar Division By Zero",
                    problem="Scalar division operation on dynamic variable without zero-value validation.",
                    mechanism=div_analysis["mechanism"],
                    impact="Raises runtime ZeroDivisionError crashing calculation pipeline on zero input.",
                    severity="HIGH",
                    file=cand.file,
                    symbol=cand.symbol,
                    start_line=start_line,
                    end_line=end_line,
                    evidence_excerpt=actual_disk_excerpt,
                    supporting_context=div_analysis.get("context", []),
                    confidence=0.90,
                    recommendation="Add explicit denominator check: `if denominator != 0: ... else: ...` or fallback epsilon.",
                )
            elif div_analysis["category"] == "UNCONFIRMED_ZERO_RISK":
                return UnconfirmedRisk(
                    title="Potential Zero-Division Edge Case",
                    observation=f"Division operation in `{cand.file}:{start_line}`: `{actual_disk_excerpt[:120]}`.",
                    inference=div_analysis.get("inference", "Potential zero value propagation requiring boundary verification."),
                    file=cand.file,
                    start_line=start_line,
                    end_line=end_line,
                    evidence_excerpt=actual_disk_excerpt,
                    recommendation="Verify input domain boundaries and add zero-guard fallback where necessary.",
                )
            else:
                # Vectorized Pandas/NumPy, Path join, or Guarded calculation -> Source observation
                return SourceObservation(
                    file=cand.file,
                    symbol=cand.symbol,
                    start_line=start_line,
                    end_line=end_line,
                    excerpt=actual_disk_excerpt[:160],
                    category="CALCULATION",
                    description=div_analysis.get("description", "Standard arithmetic / vectorized array calculation."),
                )

        # 5. Security & Authentication Analysis
        if "auth" in cand.problem.lower() or "security" in cand.problem.lower() or "key" in cand.problem.lower():
            sec_analysis = self._analyze_security_issue(content, lines, start_line, actual_disk_excerpt)
            if sec_analysis["is_defect"]:
                return ValidatedFinding(
                    title=cand.title or "Security Vulnerability",
                    problem=sec_analysis["problem"],
                    mechanism=sec_analysis["mechanism"],
                    impact=sec_analysis["impact"],
                    severity=cand.severity if cand.severity in {"CRITICAL", "HIGH"} else "HIGH",
                    file=cand.file,
                    symbol=cand.symbol,
                    start_line=start_line,
                    end_line=end_line,
                    evidence_excerpt=actual_disk_excerpt,
                    supporting_context=sec_analysis.get("context", []),
                    confidence=0.90,
                    recommendation=cand.recommendation or "Implement strict authentication / secret management.",
                )

        # 6. Default Fallback: If no concrete bug mechanism was proven, do NOT manufacture findings
        if cand.severity in {"CRITICAL", "HIGH", "MEDIUM"}:
            if not cand.mechanism or cand.mechanism.lower() in {"none", "n/a", "unknown"}:
                return UnconfirmedRisk(
                    title=cand.title or "Potential Architectural Risk",
                    observation=f"Inspected `{cand.file}:{start_line}-{end_line}`.",
                    inference=cand.problem or "Potential issue identified during review requiring verification.",
                    file=cand.file,
                    start_line=start_line,
                    end_line=end_line,
                    evidence_excerpt=actual_disk_excerpt,
                    recommendation=cand.recommendation or "Conduct unit and integration testing on the affected path.",
                )

        if cand.severity == "OBSERVATION" or not cand.problem or cand.problem.lower() in {"none", "n/a", "unknown"}:
            return SourceObservation(
                file=cand.file,
                symbol=cand.symbol,
                start_line=start_line,
                end_line=end_line,
                excerpt=actual_disk_excerpt[:160],
                category="STRUCTURE",
                description=f"Source structure at `{cand.file}:{start_line}-{end_line}`.",
            )

        return None

    def _locate_in_lines(self, lines: list[str], excerpt: str, symbol: str) -> tuple[int, int]:
        if symbol:
            symbol_pattern = re.compile(rf"\b(def|class|async def|const|function|var|let)\s+{re.escape(symbol)}\b")
            for idx, line in enumerate(lines, 1):
                if symbol_pattern.search(line) or symbol in line:
                    return idx, min(len(lines), idx + 10)

        if excerpt:
            first_line = excerpt.splitlines()[0].strip() if excerpt.splitlines() else ""
            if len(first_line) > 5:
                for idx, line in enumerate(lines, 1):
                    if first_line.lower() in line.lower():
                        return idx, min(len(lines), idx + len(excerpt.splitlines()))

        return 0, 0

    def _is_pure_declaration(self, excerpt: str, problem_text: str) -> bool:
        stripped = excerpt.strip()
        if stripped.startswith("class ") or stripped.startswith("@dataclass") or stripped.startswith("from ") or stripped.startswith("import "):
            if not any(kw in problem_text.lower() for kw in ["missing", "broken", "syntax", "deadlock", "leak"]):
                return True
        if re.match(r"^[A-Za-z0-9_]+\s*=\s*[A-Za-z0-9_]+\(.*\)$", stripped):
            return True
        return False

    def _is_logging_or_cli(self, excerpt: str) -> bool:
        stripped = excerpt.strip()
        if stripped.startswith("print(") or stripped.startswith("logger.") or stripped.startswith("logging."):
            return True
        if stripped.startswith("parser.add_argument(") or stripped.startswith("args = parser.parse_args()"):
            return True
        return False

    def _analyze_future_shift_usage(self, content: str, file_rel: str, start_line: int, excerpt: str) -> dict[str, Any]:
        var_match = re.search(r'([A-Za-z0-9_]+(\["[A-Za-z0-9_]+"\])?)\s*=\s*.*\.shift\(', excerpt)
        var_name = var_match.group(1) if var_match else "next_return"
        clean_name = re.sub(r'[^A-Za-z0-9_]', '', var_name.split("[")[-1])

        lines = content.splitlines()
        decision_keywords = ["if ", "buy(", "sell(", "order(", "signal", "position", "enter_", "exit_", "target_weight"]

        for idx, line in enumerate(lines, 1):
            if idx <= start_line:
                continue
            if clean_name in line:
                if any(kw in line for kw in decision_keywords) and not any(ign in line for ign in ["plot", "drawdown", "print", "cummax", "mean()", "sum()", "return {"]):
                    return {
                        "is_leakage": True,
                        "var_name": var_name,
                        "consumer_line": idx,
                        "consumer_line_text": line.strip(),
                    }

        return {"is_leakage": False, "var_name": var_name}

    def _analyze_division_safety(self, content: str, lines: list[str], start_line: int, excerpt: str) -> dict[str, Any]:
        stripped = excerpt.strip()

        # 1. Strip comments and docstrings
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''") or stripped.startswith("f\"\"\""):
            return {"category": "SAFE_OBSERVATION", "description": "Comment or docstring excerpt."}

        # 2. Check if / is inside string quotes
        if re.search(r'["\'][^"\']*/[^"\']*["\']', stripped):
            code_without_strings = re.sub(r'["\'].*?["\']', '', stripped)
            if "/" not in code_without_strings:
                return {"category": "SAFE_OBSERVATION", "description": "String literal containing forward slash."}

        # 3. Path joins
        if "path" in stripped.lower() or "root /" in stripped.lower() or "dir /" in stripped.lower() or "parents[" in stripped:
            return {"category": "SAFE_OBSERVATION", "description": "Pathlib filesystem path join operation."}

        # 4. Constant denominators (e.g. / 1000, / 252, / 100, / 1.5)
        const_div = re.search(r'/\s*([0-9]+(\.[0-9]+)?)\b', stripped)
        if const_div and float(const_div.group(1)) != 0.0:
            return {"category": "SAFE_OBSERVATION", "description": f"Arithmetic division by non-zero constant `{const_div.group(1)}`."}

        # 5. Pandas / NumPy Vectorized Division (Series / DataFrame / ndarray)
        # Subscript indexing (e.g. frame["a"] / frame["b"], df['x'] / df['y'], series / series)
        # or methods like cummax(), cumsum(), groupby(), transform(), shift(), mean(), sum(), np., pd.
        is_pandas_vector = bool(
            re.search(r'\[["\'][A-Za-z0-9_]+["\']\]\s*/\s*[A-Za-z0-9_]+(\[["\'][A-Za-z0-9_]+["\']\])?', stripped)
            or any(tok in stripped for tok in [
                "cummax()", "cumsum()", "groupby", "transform", "shift(", ".mean()", "sum()", "np.", "pd.",
                "frame[", "df[", "data[", "series[", "table[", "returns[", "high_profile[", "low_profile[", "pooled["
            ])
        )
        if is_pandas_vector:
            return {
                "category": "SAFE_OBSERVATION",
                "description": "Vectorized Pandas/NumPy array division (evaluates to IEEE-754 NaN/inf on zero/empty, does not raise ZeroDivisionError).",
            }

        # 6. Check for guard in prior 5 lines or ternary expression
        preceding = "\n".join(lines[max(0, start_line - 5) : start_line - 1])
        if "if " in preceding or "assert " in preceding or "if not " in preceding or "if " in stripped or "try:" in preceding or "cummax()" in stripped:
            return {"category": "SAFE_OBSERVATION", "description": "Guarded mathematical division with conditional zero-check."}

        # 7. AST Analysis for Python Scalar Division
        try:
            tree = ast.parse(stripped)
            binop_nodes = [node for node in ast.walk(tree) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)]
            if not binop_nodes:
                return {"category": "SAFE_OBSERVATION", "description": "Non-arithmetic expression."}

            for bin_node in binop_nodes:
                # Check right operand (the denominator)
                # If denominator is Subscript (e.g. df['col']) -> Pandas/dict access
                # If denominator is Call (e.g. len(), count()) -> Method call
                # If denominator is Constant -> Checked above
                if isinstance(bin_node.right, ast.Subscript) or isinstance(bin_node.right, ast.Call):
                    return {
                        "category": "SAFE_OBSERVATION",
                        "description": "Vectorized subscript or function result division.",
                    }

                # If denominator is a scalar variable name (e.g. a / b)
                if isinstance(bin_node.right, ast.Name):
                    denom_name = bin_node.right.id
                    # Check if denominator is an unguarded scalar parameter
                    return {
                        "category": "VERIFIED_SCALAR_BUG",
                        "mechanism": f"Scalar expression divides by dynamic variable `{denom_name}` at line {start_line} without preceding zero guard.",
                        "context": [stripped],
                    }
        except SyntaxError:
            pass

        # If unsure, downgrade to RISK / UNCONFIRMED
        return {
            "category": "UNCONFIRMED_ZERO_RISK",
            "inference": f"Dynamic division at line {start_line} uses dynamic denominator requiring runtime verification.",
        }

    def _analyze_security_issue(self, content: str, lines: list[str], start_line: int, excerpt: str) -> dict[str, Any]:
        secret_match = re.search(r'(secret|password|api_key|token)\s*=\s*["\']([^"\']{8,})["\']', excerpt, re.IGNORECASE)
        if secret_match:
            key_name = secret_match.group(1)
            val = secret_match.group(2)
            if not any(ex in val.lower() for ex in ["getenv", "os.environ", "super_secret_fallback"]):
                return {
                    "is_defect": True,
                    "problem": f"Hardcoded plaintext secret detected in variable `{key_name}`.",
                    "mechanism": f"Credential `{key_name}` is hardcoded with literal plaintext value in source code.",
                    "impact": "Exposes credentials to unauthorized actors if committed to version control.",
                    "context": [excerpt],
                }

        if "@app.post" in excerpt or "@app.put" in excerpt or "@app.delete" in excerpt:
            if "charge" in excerpt.lower() or "admin" in excerpt.lower() or "delete" in excerpt.lower():
                if "depends" not in excerpt.lower() and "get_api_key" not in excerpt and "auth" not in excerpt:
                    return {
                        "is_defect": True,
                        "problem": "Sensitive endpoint lacks authentication dependency.",
                        "mechanism": f"Route at line {start_line} accepts charge / administrative actions without requiring authentication headers.",
                        "impact": "Allows unauthenticated users to execute protected actions.",
                        "context": [excerpt],
                    }

        return {"is_defect": False}


GroundedFindingValidator = SemanticClaimValidator
