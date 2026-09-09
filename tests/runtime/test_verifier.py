from __future__ import annotations

from open_tulid.runtime.verification_runtime import HostCommandExecutor

from pathlib import Path

from open_tulid.domain import RequirementDefinition, Task, TransitionDefinition
from open_tulid.runtime.execution_contracts import compile_standard_execution_contract
from open_tulid.runtime.standard_contracts import STANDARD_CONTRACT_FILENAME
from open_tulid.runtime.verifier import (
    LOG_EXCERPT_CHARACTER_LIMIT,
    VERIFICATION_CLASSIFICATION_COMMAND_TIMEOUT,
    VERIFICATION_CLASSIFICATION_FAILED_BEHAVIOR,
    VERIFICATION_CLASSIFICATION_SOURCE_MUTATION,
    VERIFICATION_PASSED,
    VERIFICATION_REPORT_SCHEMA_V2,
    VERIFICATION_REQUEST_SCHEMA,
    CompletionSubmission,
    DeterministicVerifier,
    VerificationCommand,
    VerificationReport,
    command_policy_sha256,
    verification_request_from_execution_contract,
)

CONTRACT = """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: ".open-tulid/home"
commands:
  - name: z_setup
    argv: [python, check_verify.py, setup]
    working_directory: .
    timeout_seconds: 300
  - name: a_tests
    argv: [python, check_verify.py, tests]
    working_directory: backend
    timeout_seconds: 120
"""


def _contract(project_root: Path) -> Path:
    path = project_root / STANDARD_CONTRACT_FILENAME
    path.write_text(CONTRACT, encoding="utf-8")
    return path


def _transition() -> TransitionDefinition:
    return TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="Todo",
        to_state="SelfReview",
        worker="qwen",
        requires=RequirementDefinition(changed_files_required=True),
        transaction=None,
    )


def _task() -> Task:
    return Task(
        id="task-1",
        title="Add health",
        path="tasks/task-1.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Add a deterministic health endpoint.",
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "app.js").write_text("module.exports = () => 'pending';\n", encoding="utf-8")
    (repo / "backend").mkdir(exist_ok=True)
    script = "import sys\nprint('verifying ' + sys.argv[1])\nraise SystemExit(0)\n"
    (repo / "check_verify.py").write_text(script, encoding="utf-8")
    (repo / "backend" / "check_verify.py").write_text(script, encoding="utf-8")
    return repo


def _compiled(tmp_path: Path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    return compile_standard_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=_task(),
        transition=_transition(),
    )


def test_request_freezes_ordered_commands_ignoring_compile_reshuffle(tmp_path):
    compiled = _compiled(tmp_path)
    assert compiled.contract is not None
    # The request preserves the exact resolved-check order. Even if the
    # compiler re-sorts the checks, the request must not reorder them again and
    # must reflect the authored command order end to end.
    request = verification_request_from_execution_contract(
        compiled.contract,
        candidate_id="cand-1",
        candidate_manifest_sha256="candidate-sha",
        project_image_identity="wealthy-scholar@sha256:abc",
        environment_identity="host:linux-x86_64",
    )
    assert request.schema == VERIFICATION_REQUEST_SCHEMA
    assert request.candidate_id == "cand-1"
    assert request.candidate_manifest_sha256 == "candidate-sha"
    assert request.baseline_sha256 == compiled.contract.baseline_manifest.sha256
    assert request.project_image_identity == "wealthy-scholar@sha256:abc"
    assert request.environment_identity == "host:linux-x86_64"
    expected = [check.id for check in compiled.contract.resolved_checks]
    assert [c.name for c in request.commands] == expected
    # The authored contract order (z_setup before a_tests) survives compilation
    # and the frozen request; it is never re-sorted alphabetically.
    assert [c.name for c in request.commands] == ["z_setup", "a_tests"]
    by_name = {command.name: command for command in request.commands}
    assert by_name["a_tests"].working_directory == "backend"
    assert by_name["a_tests"].timeout_seconds == 120
    assert request.command_policy_sha256 == command_policy_sha256(request.commands)
    assert request.sha256


def test_command_policy_digest_sensitive_to_order_and_definition():
    forward = (
        VerificationCommand("z_setup", ("python", "setup.py")),
        VerificationCommand("a_tests", ("python", "tests.py")),
    )
    reordered = (
        VerificationCommand("a_tests", ("python", "tests.py")),
        VerificationCommand("z_setup", ("python", "setup.py")),
    )
    different = (
        VerificationCommand("z_setup", ("python", "setup.py"), timeout_seconds=600),
        VerificationCommand("a_tests", ("python", "tests.py")),
    )
    assert command_policy_sha256(forward) != command_policy_sha256(reordered)
    assert command_policy_sha256(forward) != command_policy_sha256(different)


def test_report_carries_request_policy_candidate_and_environment_identity(tmp_path):
    compiled = _compiled(tmp_path)
    assert compiled.contract is not None
    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=_repo(tmp_path),
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
        candidate_id="cand-1",
        candidate_manifest_sha256="candidate-sha",
    )
    assert result.accepted is True
    report = result.report
    assert report is not None
    assert report.schema == VERIFICATION_REPORT_SCHEMA_V2
    assert report.request_sha256
    assert report.command_policy_sha256
    assert report.candidate_id == "cand-1"
    assert report.candidate_manifest_sha256 == "candidate-sha"
    assert report.checks
    assert all(check.status == VERIFICATION_PASSED for check in report.checks)
    assert report.not_run_checks == 0
    assert report.source_mutated is not None
    assert report.baseline_sha256 == compiled.contract.baseline_manifest.sha256


def test_check_result_captures_cwd_timeout_expected_exit_timing_and_log_refs(tmp_path):
    compiled = _compiled(tmp_path)
    assert compiled.contract is not None
    report = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=_repo(tmp_path),
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
    ).report
    assert report is not None
    backend_check = next(c for c in report.checks if c.working_directory == "backend")
    assert backend_check.working_directory == "backend"
    assert backend_check.timeout_seconds == 120
    assert backend_check.expected_exit_code == 0
    assert backend_check.exit_code == 0
    assert backend_check.started_at
    assert backend_check.ended_at
    assert backend_check.duration_seconds >= 0.0
    # log_refs are part of the report contract, populated by artifact retention.
    assert hasattr(backend_check, "log_refs")


def test_inline_output_is_bounded(tmp_path):
    contract = """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: ".open-tulid/home"
commands:
  - name: verbose
    argv: [python, -c, "print('x' * 20000)"]
    working_directory: .
    timeout_seconds: 60
"""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / STANDARD_CONTRACT_FILENAME).write_text(contract, encoding="utf-8")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=_task(),
        transition=_transition(),
    )
    assert compiled.contract is not None
    report = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
    ).report
    assert report is not None
    bounded = report.checks[0].stdout
    assert len(bounded) <= LOG_EXCERPT_CHARACTER_LIMIT
    assert "omitted" in bounded


def test_empty_report_and_not_run_are_never_equated_with_success():
    # An empty report has no checks so it can never be accepted_inline.
    empty = VerificationReport(
        schema=VERIFICATION_REPORT_SCHEMA_V2,
        classification=None,
        baseline_sha256="baseline",
        post_manifest_sha256="post",
    )
    assert empty.accepted_inline is False
    assert empty.to_dict()["not_run_checks"] == 0

    # A v2 report with a never-run command is not accepted_inline either.
    partial = VerificationReport(
        schema=VERIFICATION_REPORT_SCHEMA_V2,
        classification=None,
        baseline_sha256="baseline",
        post_manifest_sha256="post",
        not_run_checks=1,
    )
    assert partial.accepted_inline is False


def test_source_mutation_classification_and_rejection(tmp_path):
    # Force a mutable-source scenario: a check that rewrites a tracked file.
    contract = """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: ".open-tulid/home"
commands:
  - name: mutate
    argv: [python, -c, "import pathlib; pathlib.Path('src/app.js').write_text('mutated')"]
    working_directory: .
    timeout_seconds: 60
"""
    project_root = tmp_path / "pm"
    project_root.mkdir()
    (project_root / STANDARD_CONTRACT_FILENAME).write_text(contract, encoding="utf-8")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=_task(),
        transition=_transition(),
    )
    assert compiled.contract is not None
    candidate_manifest = "immutable-candidate-sha"
    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
        candidate_id="cand-mut",
        candidate_manifest_sha256=candidate_manifest,
    )
    assert result.accepted is False
    assert result.report is not None
    # The post-verification digest differs after mutation of a tracked file.
    assert result.report.post_manifest_sha256 != candidate_manifest
    assert result.report.source_mutated is True
    assert any(error.code == "verification.source_mutation" for error in result.errors)


def test_failed_check_gets_failed_behavior_classification(tmp_path):
    contract = """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: ".open-tulid/home"
commands:
  - name: failing
    argv: [python, -c, "import sys; sys.exit(3)"]
    working_directory: .
    timeout_seconds: 60
"""
    project_root = tmp_path / "pf"
    project_root.mkdir()
    (project_root / STANDARD_CONTRACT_FILENAME).write_text(contract, encoding="utf-8")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=_task(),
        transition=_transition(),
    )
    assert compiled.contract is not None
    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
        candidate_id="cand-f",
        candidate_manifest_sha256="manifest-sha",
    )
    assert result.accepted is False
    assert result.report is not None
    assert result.report.classification_detail == VERIFICATION_CLASSIFICATION_FAILED_BEHAVIOR


def test_command_timeout_gets_timeout_classification(tmp_path):
    contract = """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: ".open-tulid/home"
commands:
  - name: sleep
    argv: [python, -c, "import time; time.sleep(60)"]
    working_directory: .
    timeout_seconds: 1
"""
    project_root = tmp_path / "pt"
    project_root.mkdir()
    (project_root / STANDARD_CONTRACT_FILENAME).write_text(contract, encoding="utf-8")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=_task(),
        transition=_transition(),
    )
    assert compiled.contract is not None
    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
        candidate_id="cand-t",
        candidate_manifest_sha256="manifest-sha",
    )
    assert result.accepted is False
    assert result.report is not None
    assert result.report.checks[0].status == "timeout"
    assert result.report.classification_detail == VERIFICATION_CLASSIFICATION_COMMAND_TIMEOUT


def test_deprecated_baseline_repetition_does_not_hide_actual_change(tmp_path):
    # Plan 4A: a new report must not repeat the baseline digest as the post
    # digest when the workspace source tree actually changed.
    project_root = tmp_path / "pb"
    project_root.mkdir()
    _contract(project_root)
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=_task(),
        transition=_transition(),
    )
    assert compiled.contract is not None
    baseline = compiled.contract.baseline_manifest.sha256
    # Mutate the tracked source before verifying, so the real post tree differs.
    (repo / "src" / "app.js").write_text("module.exports = () => 'changed';\n", encoding="utf-8")
    report = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
    ).report
    assert report is not None
    assert report.post_manifest_sha256 != baseline


def test_coverage_guard_records_test_and_build_discovery_changes(tmp_path):
    # Plan 4E: the report surfaces baseline-to-candidate changes on test files
    # and build/discovery configuration so review can judge whether a suite was
    # disabled, even when every global command exits zero.
    compiled = _compiled(tmp_path)
    assert compiled.contract is not None
    repo = _repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")
    (repo / "jest.config.js").write_text("module.exports = {};\n", encoding="utf-8")
    (repo / "src" / "app.js").write_text("module.exports = () => 'a';\n", encoding="utf-8")
    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(
            changed_files=("src/app.js", "tests/test_app.py", "jest.config.js"),
        ),
        execution_contract=compiled.contract,
        candidate_id="cand-cov",
    )
    assert result.accepted is True
    report = result.report
    assert report is not None
    by_path = {change.path: change for change in report.coverage_changes}
    assert by_path["tests/test_app.py"].kind == "add"
    assert by_path["jest.config.js"].kind == "add"
    # Ordinary source edits are not coverage changes; only test/discovery/build
    # surface is guarded.
    assert "src/app.js" not in by_path


def test_coverage_guard_surfaces_a_deleted_suite_for_review(tmp_path):
    # A suite removed from the candidate must be visible to review even though a
    # later global command still exits zero. Deleting discoverable tests is never
    # silently accepted as demonstrated coverage.
    repo = _repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("def test_x(): pass\n", encoding="utf-8")
    project_root = tmp_path / "pd"
    project_root.mkdir()
    _contract(project_root)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=_task(),
        transition=_transition(),
    )
    assert compiled.contract is not None
    # Candidate deletes the discovered suite file after the baseline was frozen.
    (tests_dir / "test_app.py").unlink()
    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
        candidate_id="cand-del",
    )
    assert result.accepted is True
    report = result.report
    assert report is not None
    deleted = next(
        change for change in report.coverage_changes
        if change.path == "tests/test_app.py"
    )
    assert deleted.kind == "delete"
    assert deleted.before_sha256
    assert deleted.after_sha256 is None
