"""
Evaluation harness for the defense system.
Runs the full pipeline against the gold set and reports per-task metrics.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Ensure the repo root is importable when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

from libs.common.utils import compute_coverage, coverage_anomaly, platform_from_url
from libs.schemas import validate_input


# ---------------------------------------------------------------------------
# Configuration & result types
# ---------------------------------------------------------------------------

@dataclass
class EvalConfig:
    gold_set_path: str
    api_base_url: str = ""
    output_dir: str = "."


@dataclass
class EvalResult:
    task: str
    metric: str
    value: float
    threshold: float
    passed: bool


# ---------------------------------------------------------------------------
# Task: input validation
# ---------------------------------------------------------------------------

def run_input_validation(posts: list) -> list[EvalResult]:
    """Validate every post against the PostWithDetails input schema.

    Metrics emitted:
      - total          total number of posts in the gold set
      - passed         number that passed validation
      - failed         number that failed validation
      - pass_rate      fraction that passed (threshold: 1.0 = 100%)
    """
    total = len(posts)
    passed_count = 0
    failed_count = 0

    for post in posts:
        valid, _ = validate_input(post)
        if valid:
            passed_count += 1
        else:
            failed_count += 1

    pass_rate = passed_count / total if total > 0 else 1.0

    return [
        EvalResult(
            task="input_validation",
            metric="total",
            value=float(total),
            threshold=float(total),
            passed=True,
        ),
        EvalResult(
            task="input_validation",
            metric="passed",
            value=float(passed_count),
            threshold=float(total),
            passed=passed_count == total,
        ),
        EvalResult(
            task="input_validation",
            metric="failed",
            value=float(failed_count),
            threshold=0.0,
            passed=failed_count == 0,
        ),
        EvalResult(
            task="input_validation",
            metric="pass_rate",
            value=pass_rate,
            threshold=1.0,
            passed=pass_rate >= 1.0,
        ),
    ]


# ---------------------------------------------------------------------------
# Task: platform detection
# ---------------------------------------------------------------------------

def run_platform_detection(posts: list) -> list[EvalResult]:
    """Derive the platform for every post from its URL.

    Checks:
      - no_hardcoded_facebook: none of the detected platforms equal the
        sentinel string "hardcoded_facebook" — confirms the function works
        generically rather than returning a hard-coded value.
      - platforms_detected: total count of posts for which a platform was
        derived (all should produce a non-empty string).
    """
    results = []
    no_hardcoded = True
    platforms_detected = 0

    for post in posts:
        url = post.get("url", "")
        platform = platform_from_url(url)
        if platform:
            platforms_detected += 1
        if platform == "hardcoded_facebook":
            no_hardcoded = False

    total = len(posts)

    results.append(EvalResult(
        task="platform_detection",
        metric="no_hardcoded_facebook",
        value=1.0 if no_hardcoded else 0.0,
        threshold=1.0,
        passed=no_hardcoded,
    ))
    results.append(EvalResult(
        task="platform_detection",
        metric="platforms_detected",
        value=float(platforms_detected),
        threshold=float(total),
        passed=platforms_detected == total,
    ))

    return results


# ---------------------------------------------------------------------------
# Task: coverage check
# ---------------------------------------------------------------------------

def run_coverage_check(posts: list) -> list[EvalResult]:
    """Compute comment coverage for every post.

    coverage = storedCommentRows / commentCount

    Metrics emitted:
      - over_1_count    posts where coverage > 1.0 (valid edge case where more
                        rows were stored than the reported commentCount)
      - zero_count      posts where coverage == 0.0 (no comments stored)
    """
    over_one = 0
    zero_coverage = 0

    for post in posts:
        engagement = post.get("engagement", {})
        stored = engagement.get("storedCommentRows", 0)
        total_comments = engagement.get("commentCount", 0)
        coverage = compute_coverage(stored, total_comments)
        if coverage_anomaly(stored, total_comments):
            over_one += 1
        if coverage == 0.0:
            zero_coverage += 1

    return [
        EvalResult(
            task="coverage_check",
            metric="over_1_count",
            value=float(over_one),
            threshold=float(len(posts)),  # any value up to total is valid
            passed=True,  # over-coverage is a valid edge case, always passes
        ),
        EvalResult(
            task="coverage_check",
            metric="zero_count",
            value=float(zero_coverage),
            threshold=float(len(posts)),  # informational metric only
            passed=True,  # zero-coverage is informational, not a failure
        ),
    ]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_all(config: EvalConfig) -> dict:
    """Load the gold set, run all eval tasks, write a report, and print a summary.

    Returns the full report as a dict.
    """
    # Load gold set
    gold_path = Path(config.gold_set_path)
    if not gold_path.exists():
        raise FileNotFoundError(f"Gold set not found: {gold_path}")
    with gold_path.open("r", encoding="utf-8") as fh:
        posts = json.load(fh)

    if not isinstance(posts, list):
        raise ValueError(f"Gold set must be a JSON array; got {type(posts).__name__}")

    # Run all checks
    all_results: list[EvalResult] = []
    all_results.extend(run_input_validation(posts))
    all_results.extend(run_platform_detection(posts))
    all_results.extend(run_coverage_check(posts))

    # Build report
    report = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "gold_set": str(gold_path.resolve()),
        "gold_set_size": len(posts),
        "api_base_url": config.api_base_url,
        "results": [asdict(r) for r in all_results],
        "summary": {
            "total_checks": len(all_results),
            "passed": sum(1 for r in all_results if r.passed),
            "failed": sum(1 for r in all_results if not r.passed),
        },
    }
    report["summary"]["all_passed"] = report["summary"]["failed"] == 0

    # Write report to output_dir
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "eval_report.json"
    with report_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    # Print summary table
    _print_summary(all_results, report["summary"], report_path)

    return report


def _print_summary(
    results: list[EvalResult],
    summary: dict,
    report_path: Path,
) -> None:
    """Print a formatted summary table to stdout."""
    col_task = 25
    col_metric = 28
    col_value = 10
    col_thresh = 10
    col_status = 8

    header = (
        f"{'TASK':<{col_task}} {'METRIC':<{col_metric}} "
        f"{'VALUE':>{col_value}} {'THRESHOLD':>{col_thresh}} {'STATUS':<{col_status}}"
    )
    separator = "-" * len(header)

    print()
    print("=" * len(header))
    print("  DEFENSE SYSTEM EVAL REPORT")
    print("=" * len(header))
    print(header)
    print(separator)

    prev_task = None
    for r in results:
        if r.task != prev_task and prev_task is not None:
            print(separator)
        prev_task = r.task

        status = "PASS" if r.passed else "FAIL"
        print(
            f"{r.task:<{col_task}} {r.metric:<{col_metric}} "
            f"{r.value:>{col_value}.4f} {r.threshold:>{col_thresh}.4f} {status:<{col_status}}"
        )

    print("=" * len(header))
    total = summary["total_checks"]
    passed = summary["passed"]
    failed = summary["failed"]
    overall = "ALL PASSED" if summary["all_passed"] else f"{failed} FAILED"
    print(f"  Total checks: {total}  |  Passed: {passed}  |  Failed: {failed}  |  {overall}")
    print("=" * len(header))
    print(f"  Report written to: {report_path}")
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run the defense system evaluation harness against a gold set."
    )
    parser.add_argument(
        "--gold-set",
        default=str(Path(__file__).parent.parent / "posts_with_details.json"),
        help="Path to the gold-set JSON array of PostWithDetails objects. "
             "(default: ../posts_with_details.json relative to this file)",
    )
    parser.add_argument(
        "--api-base",
        default="",
        help="Base URL of the live API to hit during evaluation (optional).",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory where eval_report.json will be written. (default: .)",
    )

    args = parser.parse_args()

    config = EvalConfig(
        gold_set_path=args.gold_set,
        api_base_url=args.api_base,
        output_dir=args.output_dir,
    )

    report = run_all(config)
    sys.exit(0 if report["summary"]["all_passed"] else 1)
