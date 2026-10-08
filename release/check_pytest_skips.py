"""Fail CI when a test was skipped that is not expected to skip on this platform.

    python -m pytest --junitxml=report.xml
    python release/check_pytest_skips.py report.xml --platform macos

A skipped test passes silently, so a CI run could look green while, say, the
real-Chrome rendering tests or the symbolic-link tests never ran. This reads the
JUnit report pytest writes and fails unless every skipped test is listed below for
the platform, and unless the report has tests at all.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Tests that legitimately cannot run on a platform, with the reason. Anything else
# that skips is a failure.
EXPECTED_SKIPS = {
    "macos": {
        "tests.test_release_packaging::test_job_object_kills_child_when_launcher_handle_closes":
            "Windows Job Object; macOS uses the parent pipe (tests/test_release_macos.py)",
    },
}


def skipped_tests(report: Path) -> tuple[int, list[str]]:
    """(number of test cases, ids of the skipped ones) from a pytest JUnit report."""
    root = ET.parse(report).getroot()
    cases = list(root.iter("testcase"))
    skipped = [f"{case.get('classname')}::{case.get('name')}" for case in cases
               if case.find("skipped") is not None]
    return len(cases), skipped


def unexpected_skips(skipped: list[str], platform: str) -> list[str]:
    expected = EXPECTED_SKIPS.get(platform, {})
    # Parametrized ids ("name[param]") are matched by their base test name.
    return [test for test in skipped if test.split("[", 1)[0] not in expected]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", type=Path, help="pytest --junitxml report")
    parser.add_argument("--platform", required=True, choices=sorted(EXPECTED_SKIPS))
    args = parser.parse_args(argv)

    if not args.report.is_file():
        print(f"skip check: report {args.report} not found")
        return 1
    total, skipped = skipped_tests(args.report)
    if total == 0:
        print("skip check: the report contains no tests")
        return 1
    unexpected = unexpected_skips(skipped, args.platform)
    for test in skipped:
        reason = EXPECTED_SKIPS[args.platform].get(test.split("[", 1)[0])
        print(("expected skip: " if reason else "UNEXPECTED SKIP: ") + test + (f"  ({reason})" if reason else ""))
    print(f"skip check: {total} tests, {len(skipped)} skipped, {len(unexpected)} unexpected")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main())
