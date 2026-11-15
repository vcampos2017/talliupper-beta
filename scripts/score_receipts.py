"""Score the parser against every receipt in the receipt library.

Usage (from the repository folder):

    python scripts/score_receipts.py                 # scoreboard
    python scripts/score_receipts.py --details       # also show what failed
    python scripts/score_receipts.py --save-baseline # remember today's results
    python scripts/score_receipts.py --compare       # show changes vs. baseline

Run it before and after a parser change. A change is an improvement only if
receipts move from FAIL to PASS without any moving the other way.

The exit code is 1 when a reviewed fixture without a recorded known issue
fails, so the script can gate a release.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from receipt_reader.receipt_library import CHECKS, LIBRARY_ROOT, score_library  # noqa: E402

BASELINE = LIBRARY_ROOT / "baseline.json"


def _status(score) -> str:
    # A draft's expected.json was copied from the parser, so it would always
    # "pass"; it means nothing until a person checks it against the photo.
    if not score.fixture.reviewed:
        return "DRAFT"
    if score.passed:
        return "PASS"
    return "KNOWN" if score.fixture.known_issue else "FAIL"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--details", action="store_true", help="show failed checks")
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--json", action="store_true", help="print machine-readable results")
    args = parser.parse_args(argv)

    scores = score_library()
    if not scores:
        print(f"No fixtures found in {LIBRARY_ROOT}")
        return 0

    results = {
        score.fixture.name: {
            "status": _status(score),
            "failed": score.failed,
            "scored": len(score.scored),
        }
        for score in scores
    }
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        width = max(len(name) for name in results) + 2
        print(f"{'Receipt'.ljust(width)}Status  Checks passed")
        for score in scores:
            scored = len(score.scored)
            passed = scored - len(score.failed)
            print(f"{score.fixture.name.ljust(width)}{_status(score):<8}{passed}/{scored}")
            if args.details:
                for check in score.failed:
                    detail = score.details.get(check, "")
                    print(f"{'':{width}}  - {check}: {detail}")
                if score.fixture.known_issue:
                    print(f"{'':{width}}  known issue: {score.fixture.known_issue}")

        by_merchant: dict[str, list[bool]] = defaultdict(list)
        for score in scores:
            if score.fixture.reviewed:
                by_merchant[score.fixture.merchant].append(score.passed)
        print()
        print("By merchant:")
        for merchant, outcomes in sorted(by_merchant.items()):
            print(f"  {merchant:<16}{sum(outcomes)}/{len(outcomes)} receipts fully correct")

        reviewed = [score for score in scores if score.fixture.reviewed]
        check_failures = {
            check: sum(1 for score in reviewed if score.checks.get(check) is False)
            for check in CHECKS
        }
        common = [(n, c) for c, n in check_failures.items() if n]
        if common:
            print()
            print("Most common failures:")
            for count, check in sorted(common, reverse=True)[:5]:
                print(f"  {check:<18}{count} receipt(s)")

        total_passed = sum(score.passed for score in reviewed)
        drafts = len(scores) - len(reviewed)
        print()
        print(f"Overall: {total_passed}/{len(reviewed)} reviewed receipts fully correct"
              + (f" ({drafts} draft(s) awaiting review)" if drafts else ""))

    if args.compare:
        if not BASELINE.exists():
            print("\nNo baseline yet. Run with --save-baseline first.")
        else:
            before = json.loads(BASELINE.read_text(encoding="utf-8"))
            changes = [
                f"  {name}: {before.get(name, {}).get('status', 'NEW')} -> {now['status']}"
                for name, now in results.items()
                if before.get(name, {}).get("status") != now["status"]
            ]
            print("\nChanges since baseline:" if changes else "\nNo changes since baseline.")
            print("\n".join(changes))

    if args.save_baseline:
        BASELINE.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        print(f"\nSaved baseline to {BASELINE.relative_to(PROJECT_ROOT)}")

    regressions = [name for name, result in results.items() if result["status"] == "FAIL"]
    return 1 if regressions else 0


if __name__ == "__main__":
    raise SystemExit(main())
