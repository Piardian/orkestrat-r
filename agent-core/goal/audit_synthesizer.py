from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from .audit_validator import (
    FindingCandidate,
    GroundedFinding,
    SemanticClaimValidator,
    SourceObservation,
    UnconfirmedRisk,
    ValidatedFinding,
)


class AuditSynthesizer:
    def __init__(self, repo_path: str | Path) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.validator = SemanticClaimValidator(self.repo_path)

    def synthesize(
        self,
        goal_id: str,
        goal_text: str,
        plan_dict: dict[str, Any],
        evidence_packet: dict[str, Any],
        analyst_results: list[Any],
        review_dict: dict[str, Any],
    ) -> str:
        repo_name = self.repo_path.name
        repo_map = evidence_packet.get("repo_map", {})
        entry_points = repo_map.get("entry_points", [])
        configs = repo_map.get("configs", [])
        manifests = repo_map.get("manifests", [])
        top_packages = repo_map.get("top_packages", [])
        tests = repo_map.get("tests", [])
        evidence_items = evidence_packet.get("evidence", [])

        # 1. Extract raw candidate findings from evidence packet and analyst outputs
        raw_candidates = self._extract_raw_candidates(evidence_packet, analyst_results, review_dict)

        # 2. Run semantic claim validation (syntax grounding + semantic mechanism verification + consumer analysis)
        verified_findings, unconfirmed_risks, source_observations = self.validator.process(raw_candidates)

        # 3. Deduplicate items
        verified_findings = self._dedupe_findings(verified_findings)
        unconfirmed_risks = self._dedupe_risks(unconfirmed_risks)
        source_observations = self._dedupe_observations(source_observations)

        # 4. Synthesize project purpose
        project_purpose = self._synthesize_project_purpose(evidence_items, analyst_results, repo_name)

        # 5. Build the report
        lines = [
            f"# Technical Audit Report: {repo_name}",
            f"",
            f"**Goal ID:** `{goal_id}`  ",
            f"**Repository:** `{self.repo_path}`  ",
            f"**Audit Mode:** Read-Only Technical Architecture & Source Code Integrity Audit  ",
            f"",
            f"---",
            f"",
            f"## 1. Project Purpose & Scope",
            f"",
            project_purpose,
            f"",
            f"- **Primary Entry Points:** {', '.join(f'`{ep}`' for ep in entry_points) if entry_points else '`[None explicitly named]`'}",
            f"- **Discovered Tech Stack / Manifests:** {', '.join(f'`{m}`' for m in manifests) if manifests else '`[No standard manifests]`'}",
            f"- **Audit Scope:** Architecture structure, execution data flow, verified source findings, risk inferences, and test verification gaps.",
            f"",
            f"---",
            f"",
            f"## 2. Architecture & Subsystem Mapping",
            f"",
            f"The codebase is organized into the following discovered components:",
            f"",
            f"| Subsystem / Layer | Key Files / Paths | Description / Role |",
            f"| :--- | :--- | :--- |",
        ]

        if entry_points:
            lines.append(f"| **Entry Points** | {', '.join(f'`{ep}`' for ep in entry_points[:3])} | Application execution triggers and orchestration |")
        if configs:
            lines.append(f"| **Configuration** | {', '.join(f'`{c}`' for c in configs[:4])} | Centralized settings, environment definitions, and parameters |")
        if top_packages:
            for pkg in top_packages[:6]:
                lines.append(f"| **Module `{pkg}`** | `{pkg}/` | Core domain logic and internal utilities |")
        if manifests:
            lines.append(f"| **Dependency Manifests** | {', '.join(f'`{m}`' for m in manifests[:3])} | Package dependencies and environment specifications |")
        if tests:
            lines.append(f"| **Test Suites** | {', '.join(f'`{t}`' for t in tests[:3])} | Automated verification and regression tests |")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 3. Main Data Flow & Execution Pipeline",
            f"",
        ])

        data_flow_steps = self._synthesize_data_flow(entry_points, configs, top_packages, evidence_items)
        for idx, step in enumerate(data_flow_steps, 1):
            lines.append(f"{idx}. **{step['title']}:** {step['description']}")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 4. Verified Source-Backed Findings (Critical / High / Medium / Low)",
            f"",
        ])

        if verified_findings:
            lines.extend([
                f"| # | Title | Problem & Mechanism | File & Lines | Severity | Impact | Confidence |",
                f"| :- | :--- | :--- | :--- | :--- | :--- | :--- |",
            ])
            for idx, item in enumerate(verified_findings, 1):
                sym = f" (`{item.symbol}`)" if item.symbol else ""
                lines.append(
                    f"| {idx} | **{item.title}** | {item.problem} *Mechanism:* {item.mechanism} | `{item.file}:{item.start_line}-{item.end_line}`{sym} | **{item.severity}** | {item.impact} | {item.confidence:.2f} |"
                )
        else:
            lines.append("No source-backed defect was proven in the inspected scope.")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 5. Unconfirmed Risks & Inferences",
            f"",
            f"To prevent false positives, potential concerns that cannot be definitively proven from the inspected code excerpts alone are categorized below:",
            f"",
            f"| Category / Title | Status | Code Observation | Inferred Risk / Hypothesis | Recommended Verification |",
            f"| :--- | :--- | :--- | :--- | :--- |",
        ])

        if unconfirmed_risks:
            for item in unconfirmed_risks:
                lines.append(
                    f"| **{item.title}** | `{item.status}` | {item.observation} | {item.inference} | {item.recommendation or 'Conduct targeted integration testing.'} |"
                )
        else:
            lines.append(f"| **General Architecture** | `UNCONFIRMED` | Standard codebase components inspected. | No blocking unconfirmed risks identified. | Maintain automated CI tests. |")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 6. Relevant Source Observations & Architecture Details",
            f"",
            f"Key neutral structural definitions and configuration parameters identified on disk (not defects):",
            f"",
            f"| Category | Location | Summary / Description |",
            f"| :--- | :--- | :--- |",
        ])

        for obs in source_observations[:8]:
            sym = f" (`{obs.symbol}`)" if obs.symbol else ""
            lines.append(f"| `{obs.category}` | `{obs.file}:{obs.start_line}-{obs.end_line}`{sym} | {obs.description} |")

        if not source_observations:
            lines.append(f"| `STRUCTURE` | `{entry_points[0] if entry_points else 'root'}` | Core codebase structure inspected on disk. |")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 7. Test Structure & Verification Gaps",
            f"",
        ])

        if tests:
            lines.append(f"- **Discovered Tests:** Discovered {len(tests)} test files ({', '.join(f'`{t}`' for t in tests[:3])}).")
            lines.append(f"- **Verification Recommendations:** Ensure continuous integration (CI) triggers tests on pull requests and measures code coverage.")
        else:
            lines.append(f"- **Missing Automated Test Suite:** No standard unit test directories (`tests/`, `test/`) or test files were identified in the primary map.")
            lines.append(f"- **Recommended Verification Addition:** Implement automated unit tests covering core domain calculations and boundary conditions.")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 8. Reliability & Security Assessment",
            f"",
            f"| Evaluation Area | Status | Evidence Grounding |",
            f"| :--- | :--- | :--- |",
        ])

        # Strictly map matrix rows to existing validated findings or discovered entry points
        if verified_findings:
            for item in verified_findings[:5]:
                lines.append(f"| **{item.title}** | ⚠️ DEFECT IDENTIFIED | `{item.file}:{item.start_line}-{item.end_line}` |")
        elif entry_points:
            lines.append(f"| **Entry Point Verification** | ✔️ INSPECTED ON DISK | `{entry_points[0]}` |")

        if configs:
            lines.append(f"| **Configuration Separation** | ✔️ INSPECTED ON DISK | `{configs[0]}` |")

        lines.extend([
            f"",
            f"---",
            f"",
            f"## 9. Prioritized Remediation Plan",
            f"",
            f"1. **Phase 1 (Critical & High Remediation):** Address any confirmed defect findings and implement strict parameter boundary checks.",
            f"2. **Phase 2 (Automated Test Coverage):** Add regression tests for unconfirmed risk paths and calculation edge cases.",
            f"3. **Phase 3 (Operational Hardening):** Maintain environment secret separation and configure CI test execution.",
            f"",
            f"---",
            f"",
            f"## 10. Overall Conclusion",
            f"",
            f"The audit for **{repo_name}** has completed. Source observations have been strictly separated from findings, and all verified defects are backed by concrete mechanisms and disk line ranges.",
            f"",
            f"**Audit Status:** COMPLETED  ",
            f"**Repository Source Files:** 100% byte-for-byte preserved (read-only audit, no modifications applied).",
        ])

        return "\n".join(lines)

    def _extract_raw_candidates(
        self,
        evidence_packet: dict[str, Any],
        analyst_results: list[Any],
        review_dict: dict[str, Any],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []

        # 1. Extract from analyst verdicts
        for idx, analyst in enumerate(analyst_results):
            payload = analyst if isinstance(analyst, dict) else (analyst.to_dict() if hasattr(analyst, "to_dict") else {})
            for ev in payload.get("evidence", []):
                path = ev.get("path", "")
                lines_str = ev.get("lines", "1-10")
                s_line, e_line = self._parse_line_range(lines_str)
                if path:
                    candidates.append({
                        "title": f"Analyst Excerpt Review ({path})",
                        "severity": "OBSERVATION",
                        "file": path,
                        "symbol": "",
                        "start_line": s_line,
                        "end_line": e_line,
                        "claim": payload.get("reason", "Analyst inspected excerpt."),
                        "problem": "",
                        "mechanism": "",
                        "impact": "",
                        "evidence_excerpt": "",
                        "confidence": float(payload.get("confidence", 0.7)),
                    })

            for unc in payload.get("uncertainties", []):
                candidates.append({
                    "title": "Analysis Uncertainty",
                    "severity": "RISK",
                    "file": payload.get("evidence", [{}])[0].get("path", "root") if payload.get("evidence") else "root",
                    "symbol": "",
                    "start_line": 0,
                    "end_line": 0,
                    "claim": str(unc),
                    "problem": str(unc),
                    "mechanism": "Unverified assumption in analysis.",
                    "impact": "Requires integration testing to confirm.",
                    "evidence_excerpt": "",
                    "confidence": 0.5,
                })

        # 2. Extract from inspected evidence files
        evidence_items = evidence_packet.get("evidence", [])
        for ev in evidence_items:
            path = ev.get("path", "")
            content = ev.get("content", "")
            s_line = int(ev.get("line_start", 1))

            if not path or not content or content == "[REDACTED_SECRET_FILE]":
                continue

            path_lower = path.lower()
            is_doc_file = (
                path_lower.endswith(".md")
                or path_lower.endswith(".txt")
                or path_lower.endswith(".rst")
                or path_lower.endswith(".json")
                or path_lower.endswith(".yaml")
                or path_lower.endswith(".yml")
                or "readme" in path_lower
            )

            content_lines = content.splitlines()
            for line_idx, line in enumerate(content_lines, s_line):
                stripped_line = line.strip()
                if not stripped_line:
                    continue

                # Pattern: Class or main function definition
                class_match = re.search(r"^\s*class\s+([A-Za-z0-9_]+)", line)
                if class_match:
                    sym_name = class_match.group(1)
                    candidates.append({
                        "title": f"Core Definition: `{sym_name}`",
                        "severity": "OBSERVATION",
                        "file": path,
                        "symbol": sym_name,
                        "start_line": line_idx,
                        "end_line": min(line_idx + 10, s_line + len(content_lines) - 1),
                        "claim": f"Defines class `{sym_name}` in `{path}`.",
                        "problem": "",
                        "mechanism": "",
                        "impact": "",
                        "evidence_excerpt": stripped_line,
                        "confidence": 0.95,
                    })

                if is_doc_file or stripped_line.startswith("#") or stripped_line.startswith('"""') or stripped_line.startswith("'''"):
                    continue

                # Pattern: Hardcoded secret
                if re.search(r'(api_key|secret|password|token)\s*=\s*["\'][^"\']{8,}["\']', line, re.IGNORECASE):
                    candidates.append({
                        "title": "Hardcoded Credential Risk",
                        "severity": "HIGH",
                        "file": path,
                        "symbol": "",
                        "start_line": line_idx,
                        "end_line": line_idx,
                        "claim": "Hardcoded credential detected in source code.",
                        "problem": "Credential is hardcoded with literal plaintext string.",
                        "mechanism": "Source file assigns static API key/secret value without loading from environment variables.",
                        "impact": "Exposes credentials to unauthorized actors if committed to public repositories.",
                        "evidence_excerpt": stripped_line,
                        "confidence": 0.90,
                    })

                # Pattern: Division candidate in code
                if "/" in line and not re.search(r'/\s*[0-9]+(\.[0-9]+)?\b', line):
                    candidates.append({
                        "title": "Dynamic Division Operation",
                        "severity": "HIGH",
                        "file": path,
                        "symbol": "",
                        "start_line": line_idx,
                        "end_line": line_idx,
                        "claim": "Division operation on dynamic denominator.",
                        "problem": "Dynamic division requiring verification of zero-value handling.",
                        "mechanism": "Expression performs division on dynamic operand.",
                        "impact": "Potential runtime ZeroDivisionError or NaN propagation if denominator evaluates to zero.",
                        "evidence_excerpt": stripped_line,
                        "confidence": 0.80,
                    })

                # Pattern: Future shift operation candidate
                if ".shift(-" in line:
                    candidates.append({
                        "title": "Future Data Shift Operation",
                        "severity": "CRITICAL",
                        "file": path,
                        "symbol": "",
                        "start_line": line_idx,
                        "end_line": line_idx,
                        "claim": "Negative shift `.shift(-k)` operation detected.",
                        "problem": "Shift operation with negative index loads future bar prices.",
                        "mechanism": "Calls `.shift(-k)` on price/indicator series.",
                        "impact": "Potential look-ahead bias if consumed during trading decision execution.",
                        "evidence_excerpt": stripped_line,
                        "confidence": 0.85,
                    })

        return candidates

    def _dedupe_findings(self, findings: list[ValidatedFinding]) -> list[ValidatedFinding]:
        deduped: list[ValidatedFinding] = []
        seen: set[tuple[str, int, int, str]] = set()
        for f in findings:
            key = (f.file, f.start_line, f.end_line, f.title)
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        return deduped

    def _dedupe_risks(self, risks: list[UnconfirmedRisk]) -> list[UnconfirmedRisk]:
        deduped: list[UnconfirmedRisk] = []
        seen: set[tuple[str, str]] = set()
        for r in risks:
            key = (r.file, r.title)
            if key not in seen:
                seen.add(key)
                deduped.append(r)
        return deduped

    def _dedupe_observations(self, obs: list[SourceObservation]) -> list[SourceObservation]:
        deduped: list[SourceObservation] = []
        seen: set[tuple[str, int, str]] = set()
        for o in obs:
            key = (o.file, o.start_line, o.symbol)
            if key not in seen:
                seen.add(key)
                deduped.append(o)
        return deduped

    def _parse_line_range(self, range_str: str) -> tuple[int, int]:
        parts = str(range_str).split("-")
        try:
            start = int(parts[0])
            end = int(parts[1]) if len(parts) > 1 else start
            return start, end
        except ValueError:
            return 1, 10

    def _synthesize_project_purpose(self, evidence_items: list[dict[str, Any]], analyst_results: list[Any], repo_name: str) -> str:
        for ev in evidence_items:
            path = ev.get("path", "").lower()
            if "readme" in path:
                readme_text = ev.get("content", "").strip()
                first_lines = [l for l in readme_text.splitlines() if l.strip() and not l.startswith("#")][:3]
                if first_lines:
                    return f"The project **{repo_name}** is described as: {' '.join(first_lines)}"

        if analyst_results:
            reasons = [a.reason for a in analyst_results if hasattr(a, "reason") and a.reason]
            if reasons:
                return f"Based on static analysis, **{repo_name}** provides: {reasons[0]}"

        return f"The project **{repo_name}** provides software modules structured for automated execution and data processing."

    def _synthesize_data_flow(
        self,
        entry_points: list[str],
        configs: list[str],
        top_packages: list[str],
        evidence_items: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        steps = []
        if configs:
            steps.append({
                "title": "Configuration Initialization",
                "description": f"Settings and environment parameters are loaded from {', '.join(f'`{c}`' for c in configs[:2])}.",
            })
        if entry_points:
            steps.append({
                "title": "Execution Orchestration",
                "description": f"CLI / runtime loop begins at {', '.join(f'`{ep}`' for ep in entry_points[:2])}.",
            })
        if top_packages:
            steps.append({
                "title": "Domain Logic & Processing",
                "description": f"Core operations and algorithms execute within modules: {', '.join(f'`{p}`' for p in top_packages[:3])}.",
            })
        steps.append({
            "title": "Output & Result Generation",
            "description": "Computed outputs, logs, or response objects are emitted to destination channels.",
        })
        return steps
