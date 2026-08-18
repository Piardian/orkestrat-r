from __future__ import annotations

import argparse
from pathlib import Path

from evidence.builder import build_evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Build deterministic repo evidence without LLM usage.")
    parser.add_argument("--repo", required=True, help="Repository path to inspect.")
    parser.add_argument("--plan", required=True, help="Search plan JSON path.")
    parser.add_argument("--output", required=True, help="Output evidence JSON path.")
    args = parser.parse_args()

    packet = build_evidence(Path(args.repo), Path(args.plan), Path(args.output))
    summary = packet["summary"]
    print("Evidence build complete")
    print(f"Searches: {summary['search_count']}")
    print(f"Files inspected: {summary['files_inspected']}")
    print(f"Lines captured: {summary['lines_captured']}")
    print(f"Tests executed: {summary['tests_executed']}")
    print(f"Output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
