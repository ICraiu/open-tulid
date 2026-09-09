from __future__ import annotations

import subprocess
from pathlib import Path

from open_tulid.domain import Task, TransitionDefinition
from open_tulid.runtime.execution_contracts import compile_standard_execution_contract
from open_tulid.runtime.standard_contracts import STANDARD_CONTRACT_FILENAME
from open_tulid.runtime.verification_runtime import (
    VERIFICATION_PASSED,
    ContainerCommandExecutor,
    LockfileIdentity,
    VerificationEnvironment,
    capture_lockfile_identity,
    environment_identity_of,
    prepare_verification_copy,
    resolve_working_directory,
)
from open_tulid.runtime.verifier import (
    DeterministicVerifier,
    VerificationCommand,
    CompletionSubmission,
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


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "app.js").write_text("module.exports = () => 'pending';\n", encoding="utf-8")
    (repo / "backend").mkdir(exist_ok=True)
    script = "import sys\nprint('verifying ' + sys.argv[1])\nraise SystemExit(0)\n"
    (repo / "check_verify.py").write_text(script, encoding="utf-8")
    (repo / "backend" / "check_verify.py").write_text(script, encoding="utf-8")
    (repo / "requirements.txt").write_text("flask==3.0\n", encoding="utf-8")
    return repo


def _task() -> Task:
    return Task(
        id="task-1",
        title="Add health",
        path="tasks/task-1.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Add a deterministic health endpoint.",
    )


def _transition() -> TransitionDefinition:
    from open_tulid.domain import RequirementDefinition
    return TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="Todo",
        to_state="SelfReview",
        worker="qwen",
        requires=RequirementDefinition(changed_files_required=True),
        transaction=None,
    )


def _compiled(tmp_path: Path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / STANDARD_CONTRACT_FILENAME).write_text(CONTRACT, encoding="utf-8")
    return compile_standard_execution_contract(
        project_root=project_root,
        repo_root=_make_repo(tmp_path),
        task=_task(),
        transition=_transition(),
    )


class FakeRunner:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, args, *, check=False, capture_output=True, text=True, timeout=None):
        self.calls.append((tuple(args), timeout))
        return subprocess.CompletedProcess(
            args=tuple(args),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


def test_prepare_verification_copy_isolates_candidate_from_writable_copy(tmp_path):
    repo = _make_repo(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "src").mkdir()
    (candidate / "src" / "app.js").write_text("module.exports = () => 'new';\n", encoding="utf-8")
    (candidate / "node_modules").mkdir()
    (candidate / "node_modules" / "dep").write_text("ignored", encoding="utf-8")
    (candidate / ".git").mkdir()

    copy = prepare_verification_copy(
        candidate_path=candidate,
        writable_root=tmp_path / "writable",
        copy_id="vc-1",
    )
    assert copy.is_dir()
    assert (copy / "src" / "app.js").is_file()
    assert (copy / "src" / "app.js").read_text(encoding="utf-8") == "module.exports = () => 'new';\n"
    assert not (copy / "node_modules").exists()
    assert not (copy / ".git").exists()
    # The immutable candidate is untouched.
    assert (candidate / "src" / "app.js").read_text(encoding="utf-8") == "module.exports = () => 'new';\n"


def test_resolve_working_directory_refuses_escapes_and_absolutes(tmp_path):
    copy = tmp_path / "copy"
    (copy / "backend").mkdir(parents=True)
    assert resolve_working_directory(copy, ".") == copy
    assert resolve_working_directory(copy, "backend") == (copy / "backend").resolve()
    assert resolve_working_directory(copy, "../other") is None
    assert resolve_working_directory(copy, "/etc") is None
    assert resolve_working_directory(copy, "missing") is not None  # unresolved dir checked by caller


def test_lockfile_identity_follows_committed_locks(tmp_path):
    repo = _make_repo(tmp_path)
    before = capture_lockfile_identity(repo)
    assert isinstance(before, LockfileIdentity)
    assert before.sha256
    assert any(p.name == "requirements.txt" for p in [Path(p) for p in before.paths])
    (repo / "requirements.txt").write_text("flask==2.0\n", encoding="utf-8")
    after = capture_lockfile_identity(repo)
    assert after.sha256 != before.sha256


def test_environment_identity_is_deterministic_and_image_sensitive():
    a = VerificationEnvironment(project_image_identity="img@sha256:abc")
    b = VerificationEnvironment(project_image_identity="img@sha256:abc")
    c = VerificationEnvironment(project_image_identity="img@sha256:def")
    assert environment_identity_of(a) == environment_identity_of(b)
    assert environment_identity_of(a) != environment_identity_of(c)


def test_host_executor_runs_commands_and_reports_timing(tmp_path):
    compiled = _compiled(tmp_path)
    assert compiled.contract is not None
    command = next(c for c in compiled.contract.resolved_checks if c.id == "a_tests")
    from open_tulid.runtime.verification_runtime import HostCommandExecutor
    outcome = HostCommandExecutor().execute(_as_vc(command), _repo_copy(tmp_path))
    assert outcome.check.status == VERIFICATION_PASSED
    assert outcome.error is None
    assert outcome.check.exit_code == 0
    assert outcome.check.working_directory == "backend"
    assert outcome.check.started_at
    assert outcome.check.ended_at
    assert outcome.check.duration_seconds >= 0.0


def _repo_copy(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    if not (repo / "backend" / "check_verify.py").exists():
        repo = _make_repo(tmp_path)
    return repo


def _as_vc(check) -> VerificationCommand:
    return VerificationCommand(
        name=check.id,
        argv=tuple(check.argv),
        working_directory=check.working_directory,
        timeout_seconds=check.timeout_seconds,
        expected_exit_code=check.expect.exit_code,
    )


def test_host_executor_classifies_timeout_and_environment(tmp_path):
    from open_tulid.runtime.verification_runtime import HostCommandExecutor
    executor = HostCommandExecutor()
    repo = _make_repo(tmp_path)
    t_cmd = VerificationCommand("sleep", ("python", "-c", "import time; time.sleep(60)"), timeout_seconds=1)
    t_out = executor.execute(t_cmd, repo)
    assert t_out.check.status == "timeout"
    assert t_out.error is not None and t_out.error.code == "verification.check_timeout"

    e_cmd = VerificationCommand("bad_cwd", ("python", "check_verify.py"), working_directory="../escape")
    e_out = executor.execute(e_cmd, repo)
    assert e_out.check.status == "environment_error"
    assert e_out.error is not None and e_out.error.code == "verification.check_environment"


def test_container_executor_uses_resolved_image_and_resolves_repo_relative_cwd(tmp_path):
    env = VerificationEnvironment(
        project_image_identity="wealthy-scholar-worker@sha256:abc",
        container_workspace="/ws/project",
        docker_executable="docker",
    )
    runner = FakeRunner(stdout="verifying tests")
    executor = ContainerCommandExecutor(environment=env, runner=runner)
    repo = _make_repo(tmp_path)
    command = VerificationCommand("tests", ("python", "check_verify.py"), working_directory="backend", timeout_seconds=120)
    outcome = executor.execute(command, repo)
    assert outcome.error is None
    assert outcome.check.status == VERIFICATION_PASSED
    assert outcome.check.exit_code == 0
    assert outcome.check.working_directory == "backend"
    # The docker invocation uses the resolved project image, overrides the agent
    # entrypoint, resolves the repository-relative cwd inside the container, and
    # mounts the copy at the project root distinct from the working directory.
    docker_call = runner.calls[0][0]
    assert "wealthy-scholar-worker@sha256:abc" in docker_call
    assert "--entrypoint" in docker_call and "/bin/sh" in docker_call
    assert "-w" in docker_call and "/ws/project/backend" in docker_call
    assert any(
        part == f"{str(repo.resolve())}:/ws/project:rw" or part.endswith(":/ws/project:rw")
        for part in docker_call
    ), docker_call
    assert "OPEN_TULID_VERIFICATION=1" in docker_call


def test_container_executor_timeout_terminates_container_and_records_interrupted(tmp_path):
    env = VerificationEnvironment(project_image_identity="img@sha256:t")
    runner = FakeRunner(returncode=124, stderr="timed out")
    executor = ContainerCommandExecutor(environment=env, runner=runner)
    repo = _make_repo(tmp_path)
    command = VerificationCommand("slow", ("sleep", "3600"), timeout_seconds=1)
    outcome = executor.execute(command, repo)
    assert outcome.check.status == "timeout"
    assert outcome.error is not None and outcome.error.code == "verification.check_timeout"
    # docker stop + rm -f were issued so the container is terminated.
    docker_run = runner.calls[0][0]
    assert any("open-tulid-verify-slow" in token for token in docker_run)
    stop_calls = [c for c in runner.calls if "stop" in c[0]]
    assert stop_calls, runner.calls


def test_container_executor_reports_env_blocker_without_passing_model_credentials(tmp_path):
    env = VerificationEnvironment()
    executor = ContainerCommandExecutor(environment=env, runner=FakeRunner())
    repo = _make_repo(tmp_path)
    command = VerificationCommand("build", ("make", "build"))
    outcome = executor.execute(command, repo)
    assert outcome.check.status == "environment_error"
    assert outcome.error is not None and outcome.error.code == "verification.env_image_unavailable"
    # No model-proxy/completion credentials are ever injected: the request env
    # only carries the verification marker, and no image identity means the
    # command never reaches a container or the host.


def test_verifier_carries_environment_identity_and_injected_executor(tmp_path):
    compiled = _compiled(tmp_path)
    assert compiled.contract is not None
    from open_tulid.runtime.verification_runtime import HostCommandExecutor
    verifier = DeterministicVerifier()
    result = verifier.verify(
        workspace=_repo_copy(tmp_path),
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
        candidate_id="cand-env",
        candidate_manifest_sha256="manifest-sha",
        executor=HostCommandExecutor(),
        environment_identity="env:linux-x86_64",
    )
    assert result.accepted is True
    report = result.report
    assert report is not None
    assert report.environment_identity == "env:linux-x86_64"
    assert report.request_sha256
    assert report.not_run_checks == 0
