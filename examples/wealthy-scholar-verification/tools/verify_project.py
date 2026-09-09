"""Project-owned global verification. Run from the Wealthy Scholar repo root.

Uses Node's test runner, pytest, and Vite; requires real discovery and execution.
No service is started here. Tests must own deterministic provider/data fixtures.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# These are project-wide coverage expectations, never writable-path limits or
# task selectors. New components must be added here in the project setup change.
# No component is deferred in the required research-product baseline.
NODE_SUITES = {
    "backend": ("backend", "tests/**/*.test.js"),
    "node_contracts": ("backend", "tests/contracts/**/*.test.js"),
    "frontend": ("frontend", "tests/**/*.test.js"),
    "integration": ("tests", "deterministic/**/*.test.js"),
}
PYTHON_SUITES = {"python_contracts": "tests/contracts", "research": "tests"}
# The audited backend already contains 17 test files. The setup baseline must
# retain those plus dedicated contracts; future baseline changes are reviewed.
MINIMUM_FILES = {"backend": 17, "research": 2}
REQUIRED_MANIFESTS = (
    "backend/package.json", "backend/package-lock.json",
    "frontend/package.json", "frontend/package-lock.json",
    "research/pyproject.toml", "research/uv.lock", "tests/package.json", "tests/package-lock.json",
)


def inventory(root: Path) -> dict[str, list[Path]]:
    """Fail before execution if the promised setup or discovery is absent."""
    missing = [f"setup required: missing {relative}" for relative in REQUIRED_MANIFESTS
               if not (root / relative).is_file()]
    suites = {}
    for name, (directory, pattern) in NODE_SUITES.items():
        suites[name] = sorted((root / directory).glob(pattern))
    for name, directory in PYTHON_SUITES.items():
        base = root / "research" / directory
        suites[name] = sorted(set(base.rglob("test_*.py")) | set(base.rglob("*_test.py")))
    for name, paths in suites.items():
        minimum = MINIMUM_FILES.get(name, 1)
        if len(paths) < minimum:
            missing.append(f"discovery missing: {name} ({len(paths)} files; expected at least {minimum})")
    if missing:
        raise ValueError("; ".join(missing))
    return suites


def run(argv: list[str], cwd: Path, log: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=600)
    log.write_text(result.stdout + "\n" + result.stderr)
    if result.returncode:
        raise ValueError(f"command failed ({result.returncode}): {argv}; log={log}")
    return result


def node_executed(output: str, path: Path) -> int:
    counts = {key: int(value) for key, value in re.findall(
        r"^# (tests|pass|fail|cancelled|skipped|todo) (\d+)\s*$", output, re.M
    )}
    # Node reports an empty file as a successful file-level test. That is not
    # evidence that any assertions were discovered, even though exit status=0.
    names = re.findall(r"^\s*ok \d+ - (.+)$", output, re.M)
    named = [name for name in names if name.strip() not in {str(path), path.name}]
    if (not named or counts.get("pass", 0) < 1 or
            any(counts.get(key, 0) for key in ("fail", "cancelled", "skipped", "todo"))):
        raise ValueError(f"discovery did not execute all tests: {path}")
    return counts["pass"]


def python_executed(report: Path) -> int:
    cases = list(ET.parse(report).iter("testcase"))
    if not cases or any(any(case.find(tag) is not None for tag in
                            ("skipped", "failure", "error")) for case in cases):
        raise ValueError(f"discovery did not execute all Python tests: {report}")
    return len(cases)


def verify(root: Path) -> int:
    evidence = root / ".open-tulid" / "project-verification"
    evidence.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        suites = inventory(root)
    except ValueError as exc:
        failure = {"status": "setup_required", "detail": str(exc)}
        (evidence / "report.json").write_text(json.dumps([failure], indent=2) + "\n")
        print(json.dumps(failure), flush=True)
        return 1
    for name, (directory, _) in NODE_SUITES.items():
        result = {"component": name, "discovered": [str(p.relative_to(root)) for p in suites[name]]}
        try:
            executed = 0
            for index, path in enumerate(suites[name]):
                output = run(["node", "--test", "--test-reporter=tap", str(path)],
                             root / directory, evidence / f"{name}-{index}.log")
                executed += node_executed(output.stdout, path)
            result.update(status="passed", executed=executed)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            result.update(status="failed", detail=str(exc))
        results.append(result)
    for name, directory in PYTHON_SUITES.items():
        result = {"component": name, "discovered": [str(p.relative_to(root)) for p in suites[name]]}
        try:
            executed = 0
            for index, path in enumerate(suites[name]):
                report = evidence / f"{name}-{index}.xml"
                report.unlink(missing_ok=True)
                # Explicitly execute every discovered file so a green neighbor
                # cannot hide an empty file or narrowed pytest discovery config.
                run(["uv", "run", "--frozen", "--no-sync", "pytest", str(path),
                     "-q", f"--junitxml={report}"], root / "research", evidence / f"{name}-{index}.log")
                executed += python_executed(report)
            result.update(status="passed", executed=executed)
        except (OSError, subprocess.TimeoutExpired, ValueError, ET.ParseError) as exc:
            result.update(status="failed", detail=str(exc))
        results.append(result)
    result = {"component": "frontend_build"}
    try:
        # Build output stays on the declared ephemeral snapshot surface. The
        # source, config, manifests and locks are still checked for mutation.
        output = evidence / "frontend-build"
        import shutil
        shutil.rmtree(output, ignore_errors=True)
        run(["npm", "run", "build", "--", "--outDir", str(output)],
            root / "frontend", evidence / "frontend-build.log")
        if not (output / "index.html").is_file() or not list(output.rglob("*.js")):
            raise ValueError("frontend build did not emit index.html and JavaScript")
        result.update(status="passed")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        result.update(status="failed", detail=str(exc))
    results.append(result)
    (evidence / "report.json").write_text(json.dumps(results, indent=2) + "\n")
    for result in results:
        print(json.dumps(result), flush=True)
    return int(any(result["status"] != "passed" for result in results))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", action="store_true", help="Check setup/discovery without running project code")
    args = parser.parse_args()
    root = Path.cwd().resolve()
    if args.inventory:
        try:
            found = inventory(root)
        except ValueError as exc:
            print(json.dumps({"status": "setup_required", "detail": str(exc)}))
            sys.exit(1)
        print(json.dumps({name: [str(p.relative_to(root)) for p in paths] for name, paths in found.items()}))
        sys.exit(0)
    sys.exit(verify(root))
