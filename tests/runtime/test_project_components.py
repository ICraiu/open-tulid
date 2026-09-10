"""Project-owned discovery through the real global contract and verifier."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from open_tulid.domain import RequirementDefinition, Task, TransitionDefinition
from open_tulid.runtime.execution_contracts import compile_standard_execution_contract
from open_tulid.runtime.verification_runtime import HostCommandExecutor
from open_tulid.runtime.verifier import CompletionSubmission, DeterministicVerifier

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/wealthy-scholar-verification"
spec = importlib.util.spec_from_file_location("project_verify", EXAMPLE / "tools/verify_project.py")
project_verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(project_verify)


@pytest.fixture
def project(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLE / "tools", repo / "tools")
    files = {
        "backend/tests/unit.test.js": "passed",
        "backend/tests/contracts/parity.test.js": "passed",
        "frontend/tests/view.test.js": "passed",
        "tests/deterministic/journey.test.js": "passed",
        "research/tests/contracts/test_parity.py": "passed",
        "research/tests/test_pipeline.py": "passed",
    }
    files.update({f"backend/tests/baseline-{index}.test.js": "passed" for index in range(15)})
    files.update({path: "{}" for path in project_verify.REQUIRED_MANIFESTS})
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    toolchain = tmp_path / "bin"
    toolchain.mkdir()
    script = (ROOT / "tests/fixtures/project_components/tool.py").read_text()
    for name in ("node", "npm", "uv", "mongod"):
        path = toolchain / name
        path.write_text("#!" + sys.executable + "\n" + script.split("\n", 1)[1])
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(toolchain) + os.pathsep + os.environ["PATH"])
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    # Use the migration's exact ordered policy, with its interpreter resolved
    # to this deterministic test environment. Production uses the project image.
    policy = (EXAMPLE / "contract.yaml").read_text().replace("[python3,", "[" + sys.executable + ",")
    (tracker / "contract.yaml").write_text(policy)
    return repo, tracker


def verify(project, *, review=False):
    repo, tracker = project
    transition = TransitionDefinition(
        id="SelfReview" if review else "ImplementTask", task_type="ImplementationTask",
        from_state="Review" if review else "Todo", to_state="Done" if review else "Review", worker="configured-worker", requires=RequirementDefinition(), transaction=None,
    )
    task = Task(id="task", title="Project task", path="tasks/task.md",
                current_state=transition.from_state, task_type="ImplementationTask")
    compiled = compile_standard_execution_contract(project_root=tracker, repo_root=repo,
                                                   task=task, transition=transition)
    assert compiled.accepted, compiled.errors
    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo, transition=transition, submission=CompletionSubmission(),
        execution_contract=compiled.contract,
    )
    return result


def test_all_components_execute_for_implementation_and_review(project):
    implementation = verify(project)
    assert implementation.accepted, implementation.errors
    review = verify(project, review=True)
    assert review.accepted, review.errors
    assert implementation.report.command_policy_sha256 == review.report.command_policy_sha256
    report = json.loads((project[0] / ".open-tulid/project-verification/report.json").read_text())
    assert {item["component"] for item in report} == {
        "backend", "node_contracts", "frontend", "integration", "python_contracts", "research", "frontend_build",
    }
    assert all(item["status"] == "passed" for item in report)
    assert all(item["executed"] > 0 for item in report if item["component"] != "frontend_build")


@pytest.mark.parametrize("relative", [
    "backend/tests/unit.test.js", "backend/tests/contracts/parity.test.js",
    "frontend/tests/view.test.js", "tests/deterministic/journey.test.js",
    "research/tests/contracts/test_parity.py", "research/tests/test_pipeline.py",
])
@pytest.mark.parametrize("behavior", ["broken", "empty", "skipped"])
def test_each_component_must_execute_real_tests(project, relative, behavior):
    (project[0] / relative).write_text(behavior)
    result = verify(project)
    # For Python a nonempty neighboring suite must not hide disabled files.
    assert not result.accepted


@pytest.mark.parametrize("directory", ["backend", "research", "frontend", "tests"])
def test_required_component_deletion_fails(project, directory):
    shutil.rmtree(project[0] / directory)
    result = verify(project)
    assert not result.accepted
    assert "setup required" in result.report.checks[-1].stdout


@pytest.mark.parametrize("behavior", ["broken", "empty"])
def test_frontend_build_failure_or_disabled_script_fails(project, behavior):
    (project[0] / "frontend/package.json").write_text(json.dumps({"build": behavior}))
    assert not verify(project).accepted


def test_new_tests_are_discovered_without_task_command_changes(project):
    before = verify(project)
    (project[0] / "backend/tests/new.test.js").write_text("broken")
    after = verify(project)
    assert before.accepted and not after.accepted
    assert before.report.command_policy_sha256 == after.report.command_policy_sha256


def test_real_node_empty_file_and_skipped_test_are_not_coverage(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable; scripted toolchain cases still run")
    path = tmp_path / "empty.test.js"
    for content in ("", "require('node:test').test.skip('disabled', () => {})"):
        path.write_text(content)
        result = subprocess.run([node, "--test", "--test-reporter=tap", str(path)], capture_output=True, text=True, check=True)
        with pytest.raises(ValueError, match="discovery"):
            project_verify.node_executed(result.stdout, path)
    path.write_text("require('node:test').test('actual', () => require('node:assert').ok(true))")
    result = subprocess.run([node, "--test", "--test-reporter=tap", str(path)], capture_output=True, text=True, check=True)
    assert project_verify.node_executed(result.stdout, path) == 1


def test_additional_declared_component_is_required_by_global_entrypoint(project):
    script = project[0] / "tools/verify_project.py"
    script.write_text(script.read_text().replace(
        'NODE_SUITES = {', 'NODE_SUITES = {"ingestion": ("ingestion", "tests/**/*.test.js"),'
    ))
    missing = verify(project)
    assert not missing.accepted
    assert "ingestion" in missing.report.checks[-1].stdout
    tests = project[0] / "ingestion/tests"
    tests.mkdir(parents=True)
    (tests / "import.test.js").write_text("passed")
    assert verify(project).accepted
    (tests / "import.test.js").write_text("broken")
    assert not verify(project).accepted


def test_backend_discovery_cannot_shrink_below_project_baseline(project):
    (project[0] / "backend/tests/unit.test.js").unlink()
    result = verify(project)
    assert not result.accepted
    assert "expected at least 17" in result.report.checks[-1].stdout
