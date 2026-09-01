#!/usr/bin/env python3

"""Test harness for bazelrc-graph.

Each directory under tests/cases/ is a test case: it contains a root `.bazelrc`
and an `expected.dot`. A case may also contain an `expected_warnings.txt` with
the warning lines the tool should print.

./tests/run_tests.py            # run all cases
./tests/run_tests.py simple     # run one case
./tests/run_tests.py --update   # regenerate expected files
"""

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
TOOL = os.path.join(ROOT_DIR, "bazelrc-graph")
CASES_DIR = os.path.join(TESTS_DIR, "cases")


def run_case(case_dir, update):
    name = os.path.basename(case_dir)
    root_rc = os.path.join(case_dir, ".bazelrc")
    if not os.path.isfile(root_rc):
        print(f"FAIL {name}: no .bazelrc in case directory")
        return False

    with tempfile.TemporaryDirectory() as out_dir:
        result = subprocess.run(
            [
                sys.executable,
                TOOL,
                "--no-png",
                "--workspace",
                case_dir,
                "-o",
                out_dir,
                root_rc,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            print(f"FAIL {name}: tool exited {result.returncode}")
            sys.stdout.write(result.stderr)
            return False
        with open(os.path.join(out_dir, "bazelrc-graph.dot")) as handle:
            actual_dot = handle.read()

    warnings = [
        line for line in result.stderr.splitlines() if line.startswith("warning:")
    ]
    actual_warnings = "".join(line + "\n" for line in warnings)

    expected_dot_path = os.path.join(case_dir, "expected.dot")
    expected_warnings_path = os.path.join(case_dir, "expected_warnings.txt")

    if update:
        with open(expected_dot_path, "w") as handle:
            handle.write(actual_dot)
        if actual_warnings:
            with open(expected_warnings_path, "w") as handle:
                handle.write(actual_warnings)
        elif os.path.exists(expected_warnings_path):
            os.remove(expected_warnings_path)
        print(f"UPDATED {name}")
        return True

    ok = True
    if not os.path.isfile(expected_dot_path):
        print(f"FAIL {name}: missing expected.dot (run with --update)")
        ok = False
    else:
        with open(expected_dot_path) as handle:
            expected_dot = handle.read()
        if actual_dot != expected_dot:
            print(f"FAIL {name}: dot output differs")
            sys.stdout.writelines(
                difflib.unified_diff(
                    expected_dot.splitlines(keepends=True),
                    actual_dot.splitlines(keepends=True),
                    fromfile="expected.dot",
                    tofile="actual.dot",
                )
            )
            ok = False

    expected_warnings = ""
    if os.path.isfile(expected_warnings_path):
        with open(expected_warnings_path) as handle:
            expected_warnings = handle.read()
    if actual_warnings != expected_warnings:
        print(f"FAIL {name}: warnings differ")
        sys.stdout.writelines(
            difflib.unified_diff(
                expected_warnings.splitlines(keepends=True),
                actual_warnings.splitlines(keepends=True),
                fromfile="expected_warnings.txt",
                tofile="actual warnings",
            )
        )
        ok = False

    if ok:
        print(f"PASS {name}")
    return ok


def render_smoke_test(case_dir, output_format):
    """Render one case to check the dot output is valid graphviz."""
    if shutil.which("dot") is None:
        print(f"SKIP {output_format} smoke test: graphviz 'dot' not installed")
        return True
    name = os.path.basename(case_dir)
    with tempfile.TemporaryDirectory() as out_dir:
        format_args = ["--svg"] if output_format == "svg" else []
        result = subprocess.run(
            [
                sys.executable,
                TOOL,
                *format_args,
                "--workspace",
                case_dir,
                "--output-dir",
                out_dir,
                os.path.join(case_dir, ".bazelrc"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        image = os.path.join(out_dir, "bazelrc-graph." + output_format)
        if result.returncode != 0 or not os.path.isfile(image):
            print(f"FAIL {output_format} smoke test ({name}): no image produced")
            sys.stdout.write(result.stderr)
            return False
    print(f"PASS {output_format} smoke test ({name})")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="regenerate the expected files from current tool output",
    )
    parser.add_argument(
        "cases",
        nargs="*",
        help="specific case names to run (default: all)",
    )
    args = parser.parse_args()

    all_cases = sorted(
        entry
        for entry in os.listdir(CASES_DIR)
        if os.path.isdir(os.path.join(CASES_DIR, entry))
    )
    cases = args.cases or all_cases
    unknown = set(cases) - set(all_cases)
    if unknown:
        parser.error("unknown cases: {}".format(", ".join(sorted(unknown))))

    ok = True
    for case in cases:
        ok = run_case(os.path.join(CASES_DIR, case), args.update) and ok
    if not args.update and cases:
        first_case = os.path.join(CASES_DIR, cases[0])
        ok = render_smoke_test(first_case, "png") and ok
        ok = render_smoke_test(first_case, "svg") and ok

    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
