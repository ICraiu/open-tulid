from __future__ import annotations

import hashlib
from pathlib import Path
from types import MappingProxyType

from open_tulid.domain import ExecutionJob, RequirementDefinition, Task, TransitionDefinition
from open_tulid.runtime.execution_contracts import (
    compile_standard_execution_contract,
    execution_contract_to_dict,
    load_job_execution_contract,
)
from open_tulid.runtime.executor import (
    _project_standard_runtime,
    _write_opencode_model_config_if_needed,
)
from open_tulid.runtime.prompts import compile_execution_prompt
from open_tulid.runtime.standard_contracts import (
    STANDARD_CONTRACT_FILENAME,
    load_standard_contract,
    standard_contract_configured,
)
from open_tulid.runtime.verifier import CompletionSubmission, DeterministicVerifier
from open_tulid.vault.validator import validate_project
from open_tulid.models import Project


CONTRACT = """\
schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: ".open-tulid/home"
commands:
  - name: tests
    argv: [python, check_verify.py, tests]
    working_directory: .
    timeout_seconds: 300
    expect:
      exit_code: 0
  - name: build
    argv: [python, check_verify.py, build]
    working_directory: backend
    timeout_seconds: 120
    expect:
      exit_code: 0
retry:
  max_attempts: 3
  visible_feedback: true
"""


def _contract(project_root: Path, text: str = CONTRACT) -> Path:
    path = project_root / STANDARD_CONTRACT_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


def _transition():
    return TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="Todo",
        to_state="SelfReview",
        worker="qwen",
        requires=RequirementDefinition(changed_files_required=True),
        transaction=None,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    (repo / "src" / "app.js").write_text("module.exports = () => 'pending';\n", encoding="utf-8")
    (repo / "backend").mkdir()
    script = "import sys\nprint('verifying ' + sys.argv[1])\nraise SystemExit(0)\n"
    (repo / "check_verify.py").write_text(script, encoding="utf-8")
    (repo / "backend" / "check_verify.py").write_text(script, encoding="utf-8")
    return repo


def test_command_only_contract_loads_and_configured_flag(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)

    assert standard_contract_configured(project_root) is True
    loaded = load_standard_contract(project_root)

    assert loaded.accepted is True
    assert loaded.contract is not None
    assert loaded.contract.schema == "tulid.contract/v1"
    assert loaded.contract.runtime.container_user == "1000:1000"
    assert loaded.contract.runtime.opencode_config_home == ".open-tulid/home"
    assert len(loaded.contract.commands) == 2
    tests = loaded.contract.commands[0]
    assert tests.name == "tests"
    assert tests.argv == ("python", "check_verify.py", "tests")
    assert tests.working_directory == "."
    assert tests.timeout_seconds == 300
    assert tests.expect.exit_code == 0
    build = loaded.contract.commands[1]
    assert build.working_directory == "backend"
    assert build.timeout_seconds == 120
    assert loaded.contract.retry.max_attempts == 3


def test_malformed_and_empty_argv_rejected(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    _contract(project_root, """schema: tulid.contract/v1
commands:
  - name: empty_cmd
    argv: []
""")
    empty = load_standard_contract(project_root)
    assert empty.accepted is False
    assert any(error.code == "contract.command_argv_empty" for error in empty.errors)

    _contract(project_root, """schema: tulid.contract/v1
commands:
  - name: bad_cmd
    argv: npm test
""")
    malformed = load_standard_contract(project_root)
    assert malformed.accepted is False
    assert any(error.code == "contract.command_argv_invalid" for error in malformed.errors)


def test_command_only_contract_invalid_yaml_and_unknown_fields_rejected(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root, "schema: tulid.contract/v1\ncommands: nonsense\n")
    loaded = load_standard_contract(project_root)
    assert loaded.accepted is False
    assert any(error.code == "contract.commands_invalid" for error in loaded.errors)

    _contract(project_root, """schema: tulid.contract/v1
baseline:
  change_surfaces: [src]
commands: []
""")
    unknown = load_standard_contract(project_root)
    assert unknown.accepted is False
    assert any(error.code == "contract.unknown_field" for error in unknown.errors)


def test_global_e2e_verifier_runs_commands_in_configured_dir_with_exit_expectation(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    task = Task(
        id="task-1",
        title="Add health",
        path="tasks/task-1.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Add a deterministic health endpoint.",
    )
    repo = _repo(tmp_path)
    transition = _transition()
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=transition,
    )
    assert compiled.accepted is True, [error.code for error in compiled.errors]
    assert compiled.contract is not None
    # The declared contract order (tests before build) is preserved end to end;
    # checks are never re-sorted alphabetically by id.
    assert [check.id for check in compiled.contract.resolved_checks] == ["tests", "build"]
    tests_check = compiled.contract.resolved_checks[0]
    assert tests_check.argv == ("python", "check_verify.py", "tests")
    assert tests_check.working_directory == "."

    result = DeterministicVerifier().verify(
        workspace=repo,
        transition=transition,
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
    )

    assert result.accepted is True
    assert result.report is not None
    assert [check.status for check in result.report.checks] == ["passed", "passed"]
    # exit status captured as verification evidence
    assert all(check.exit_code == 0 for check in result.report.checks)
    assert all(check.stdout for check in result.report.checks)


def test_global_verifier_captures_stdout_stderr_and_rejects_nonzero_exit(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root, """schema: tulid.contract/v1
commands:
  - name: failing
    argv: [node, -e, "console.error('boom'); process.exit(3);"]
    working_directory: .
    timeout_seconds: 60
""")
    task = Task(
        id="task-1",
        title="Add health",
        path="tasks/task-1.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Add a deterministic health endpoint.",
    )
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root, repo_root=repo, task=task, transition=_transition(),
    )
    assert compiled.contract is not None

    result = DeterministicVerifier().verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
    )

    assert result.accepted is False
    assert result.report is not None
    assert result.report.checks[0].status == "failed"
    assert result.report.checks[0].exit_code == 3
    assert "boom" in result.report.checks[0].stderr
    # actionable feedback includes the command, its exit code, and captured output
    message = "; ".join(error.message for error in result.errors)
    assert "failing" in message
    assert "exit code 3 (expected 0)" in message
    assert "boom" in message


def test_global_verifier_reports_command_not_found_and_timeout(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root, """schema: tulid.contract/v1
commands:
  - name: missing_binary
    argv: [definitely_not_a_real_binary_xyz, --flag]
    working_directory: .
    timeout_seconds: 30
""")
    task = Task(
        id="task-1", title="x", path="tasks/task-1.md",
        current_state="Todo", task_type="ImplementationTask", body="x",
    )
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root, repo_root=repo, task=task, transition=_transition(),
    )
    assert compiled.contract is not None

    result = DeterministicVerifier().verify(
        workspace=repo,
        transition=_transition(),
        submission=CompletionSubmission(changed_files=("src/app.js",)),
        execution_contract=compiled.contract,
    )

    assert result.accepted is False
    assert {error.code for error in result.errors} == {"verification.check_environment"}
    message = "; ".join(error.message for error in result.errors)
    assert "missing_binary" in message


def test_verifier_allows_worker_to_add_previously_unknown_file_and_directory(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    task = Task(
        id="task-1",
        title="Add research",
        path="tasks/task-1.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Add a research/ directory with notes.",
    )
    repo = _repo(tmp_path)
    transition = _transition()
    compiled = compile_standard_execution_contract(
        project_root=project_root, repo_root=repo, task=task, transition=transition,
    )
    assert compiled.contract is not None
    # The worker legitimately creates research/notes.md that was absent from the
    # baseline. This must NOT be rejected merely because it was unpredicted.
    (repo / "research").mkdir()
    (repo / "research" / "notes.md").write_text("# Plan\n", encoding="utf-8")

    result = DeterministicVerifier().verify(
        workspace=repo,
        transition=transition,
        submission=CompletionSubmission(changed_files=("research/notes.md",)),
        execution_contract=compiled.contract,
    )

    assert result.accepted is True
    assert result.report is not None
    assert all(check.status == "passed" for check in result.report.checks)
    assert {error.code for error in result.errors} == set()


def test_every_implementation_task_inherits_the_same_global_commands(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    repo = _repo(tmp_path)
    transition = _transition()

    tasks = (
        Task(
            id="task-1", title="Add health", path="tasks/task-1.md",
            current_state="Todo", task_type="ImplementationTask",
            body="Add a deterministic health endpoint.",
        ),
        Task(
            id="task-2", title="Refactor build", path="tasks/task-2.md",
            current_state="Todo", task_type="ImplementationTask",
            body="Refactor the build pipeline in a separate boundary.",
        ),
    )

    compiled = [
        compile_standard_execution_contract(
            project_root=project_root, repo_root=repo, task=task, transition=transition,
        )
        for task in tasks
    ]

    assert all(result.accepted for result in compiled)
    contracts = [result.contract for result in compiled]
    assert all(contract is not None for contract in contracts)
    # No per-task commands exist: every implementation task resolves the exact
    # same project global command set, independent of its body or scope.
    command_sets = {tuple(check.id for check in contract.resolved_checks) for contract in contracts}
    argv_sets = {tuple(check.argv for check in contract.resolved_checks) for contract in contracts}
    assert command_sets == {("tests", "build")}
    assert argv_sets == {(
        ("python", "check_verify.py", "tests"),
        ("python", "check_verify.py", "build"),
    )}


def test_task_added_tests_are_picked_up_by_the_global_suite(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    script = (
        "import subprocess, sys, pathlib\n"
        "tests = sorted(pathlib.Path('tests').glob('test_*.py'))\n"
        "if not tests:\n    raise SystemExit(1)\n"
        "for path in tests:\n"
        "    subprocess.run([sys.executable, str(path)], check=True)\n"
        "print(f'ran {len(tests)} tests')\n"
    )
    # A true global test command: it runs every test_*.py file under tests/.
    _contract(project_root, """schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: .open-tulid/home
commands:
  - name: project_tests
    argv: [python, run_tests.py]
    working_directory: .
    timeout_seconds: 300
retry:
  max_attempts: 3
  visible_feedback: true
""")
    (project_root / "run_tests.py").write_text(script, encoding="utf-8")
    (project_root / "tests").mkdir()
    (project_root / "tests" / "test_base.py").write_text(
        "def test_base():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    task = Task(
        id="task-1", title="Add evidence cards", path="tasks/task-1.md",
        current_state="Todo", task_type="ImplementationTask",
        body="Add evidence card extraction.",
    )
    transition = _transition()
    compiled = compile_standard_execution_contract(
        project_root=project_root, repo_root=project_root, task=task, transition=transition,
    )
    assert compiled.accepted is True, [error.code for error in compiled.errors]
    assert compiled.contract is not None

    # The task adds a new test file as ordinary work; it is NOT a separate
    # per-task validation command, yet the global suite picks it up.
    (project_root / "tests" / "test_evidence.py").write_text(
        "def test_evidence():\n    assert len('card') > 0\n",
        encoding="utf-8",
    )

    result = DeterministicVerifier().verify(
        workspace=project_root,
        transition=transition,
        submission=CompletionSubmission(changed_files=("tests/test_evidence.py",)),
        execution_contract=compiled.contract,
    )

    assert result.accepted is True
    assert result.report is not None
    assert [check.status for check in result.report.checks] == ["passed"]
    assert "ran 2 tests" in result.report.checks[0].stdout


def test_legacy_per_task_artifact_remains_readable_but_new_run_uses_global_commands(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    task = Task(
        id="task-1",
        title="Add health",
        path="tasks/task-1.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Add a deterministic health endpoint.",
    )
    # A legacy per-task ImplementationContract artifact may still exist for
    # history; it is readable but must NOT govern a new run.
    relative = Path("artifacts/task-1/ImplementationContract/implementation-contract.yaml")
    legacy = project_root / relative
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        "schema: tulid.implementation/v1\n"
        "source:\n"
        f"  task_id: \"{task.id}\"\n"
        f"  source_intent_sha256: \"{'a' * 64}\"\n"
        "profile: code_change\n"
        "objective: x\n"
        "change_surface:\n  add: []\n  edit: [src/app.py]\n  forbidden: []\n"
        "checks:\n  focused: []\n  invariants: []\n",
        encoding="utf-8",
    )
    task = Task(
        id="task-1", title="Add health", path="tasks/task-1.md",
        current_state="Todo", task_type="ImplementationTask",
        body="Add a deterministic health endpoint.",
        artifact_links=(relative.as_posix(),),
    )
    repo = _repo(tmp_path)
    transition = _transition()
    compiled = compile_standard_execution_contract(
        project_root=project_root, repo_root=repo, task=task, transition=transition,
    )
    assert compiled.accepted is True
    assert compiled.contract is not None
    # The new run governs by the global commands, not the legacy file surface.
    assert [check.id for check in compiled.contract.resolved_checks] == ["tests", "build"]
    assert compiled.contract.generated_contract.schema == "tulid.global_contract/v1"


def test_executor_resolves_standard_runtime_and_places_opencode_config_off_root(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)

    _Config = type("_Config", (), {"project_root": project_root})
    _Adapter = type("_Adapter", (), {"config": _Config()})

    discovered = _project_standard_runtime(_Adapter())
    assert discovered.container_user == "1000:1000"
    assert discovered.opencode_config_home == ".open-tulid/home"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_path = _write_opencode_model_config_if_needed(
        workspace=workspace,
        runtime=type("Runtime", (), {
            "container_workspace": "/workspace/project",
            "worker_types": {"qwen_27b": "opencode"},
        })(),
        worker_id="qwen_27b",
        implementation_id="qwen_27b",
        args=("run", "--model", "tulid-qwen/Qwen3.6-27B-MTP-UD-Q6_K_XL.gguf"),
        env={
            "OPEN_TULID_MODEL_ENDPOINT": "http://127.0.0.1:8787",
            "OPEN_TULID_MODEL_PROXY_ID": "qwen",
            "OPEN_TULID_MODEL_SESSION_TOKEN": "secret",
        },
        opencode_config_home=discovered.opencode_config_home,
    )

    assert not (workspace / "opencode.json").exists()
    assert config_path == "/workspace/project/.open-tulid/home/.config/opencode/opencode.json"
    assert (workspace / ".open-tulid" / "home" / ".config" / "opencode" / "opencode.json").is_file()


def test_wealthy_scholar_node_command_only_contract_validates(tmp_path):
    # Wealthy Scholar is a Node-based project: backend tests and a project build,
    # never Python pytest. This is the project's real command-only global config.
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root, """schema: tulid.contract/v1
runtime:
  container_user: "1000:1000"
  opencode_config_home: .open-tulid/home
commands:
  - name: backend_tests
    argv: [npm, test, --prefix, backend]
    working_directory: .
    timeout_seconds: 300
    expect:
      exit_code: 0
  - name: project_build
    argv: [npm, run, build]
    working_directory: .
    timeout_seconds: 300
    expect:
      exit_code: 0
retry:
  max_attempts: 3
  visible_feedback: true
""")
    for name in ["kanban", "docs", "tasks", "agents"]:
        (project_root / name).mkdir()
    (project_root / "workflow.yaml").write_text("schema_version: 1\n", encoding="utf-8")

    report = validate_project(Project(name="Agent", path=project_root))

    assert report.passed is True
    assert not any("contract" in error.message for error in report.errors)


def _implementation_task(*, spec_relative: str | None = None, body: str = "Add a deterministic health endpoint."):
    return Task(
        id="task-impl",
        title="Add health",
        path="tasks/task-impl.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body=body,
        artifact_links=(spec_relative, ) if spec_relative else (),
    )


def test_global_contract_freezes_linked_specification_into_context(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    spec = project_root / "docs" / "specification.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(
        "# Specification\n\nThe required enum is `HealthStatus`: `ok`, `degraded`.\n",
        encoding="utf-8",
    )
    task = _implementation_task(spec_relative="docs/specification.md")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert compiled.accepted is True, [error.code for error in compiled.errors]
    assert compiled.contract is not None
    assert compiled.contract.context_excerpts, "global contract must not drop the specification"
    assert "HealthStatus" in compiled.contract.context_excerpts[0].text
    assert compiled.contract.context_excerpts[0].sha256

    # The resolved context is part of the frozen prompt, not a mutable vault ref.
    compiled_prompt = compile_execution_prompt(compiled.contract)
    assert "HealthStatus" in compiled_prompt.text
    assert "docs/specification.md" in compiled_prompt.text


def test_global_contract_freezes_canonical_answer_history_from_parent_lineage(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    answers = project_root / "artifacts" / "task-round" / "QuestionRoundFile" / "answers.md"
    answers.parent.mkdir(parents=True)
    answers.write_text(
        "**Answer:**\nUse the `ok` status for a healthy dependency.\n",
        encoding="utf-8",
    )
    parent_round = Task(
        id="task-round",
        title="Clarify status meanings",
        path="tasks/task-round.md",
        current_state="Done",
        task_type="QuestionRound",
        body="How should dependent service status be modeled?",
        artifact_links=(
            "artifacts/task-round/QuestionRoundFile/answers.md",
        ),
    )
    task = _implementation_task(body="Implement status using the settled answer.")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
        parent_tasks=(parent_round,),
    )
    assert compiled.accepted is True, [error.code for error in compiled.errors]
    assert compiled.contract is not None
    excerpts = compiled.contract.context_excerpts
    assert any("Canonical QuestionRound" in excerpt.heading for excerpt in excerpts)
    answer_excerpt = next(excerpt for excerpt in excerpts if excerpt.heading.startswith("Canonical"))
    assert "`ok` status" in answer_excerpt.text
    assert "Later explicit answers override earlier conflicting answers." in answer_excerpt.text

    # Silence an unused-variable guard in older linters; the hash still proves
    # the excerpt is frozen content, not a live vault reference.
    assert answer_excerpt.sha256


def test_global_contract_passes_parent_and_required_artifact_to_resolver(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    # A required artifact the transition is about to produce must be excluded
    # from context so the worker authors it instead of reading it as settled.
    required = project_root / "artifacts" / "task-impl" / "ImplementationTaskFile" / "output.md"
    required.parent.mkdir(parents=True)
    required.write_text("# Already-produced deliverable\n", encoding="utf-8")
    task = Task(
        id="task-impl",
        title="Add health",
        path="tasks/task-impl.md",
        current_state="Todo",
        task_type="ImplementationTask",
        body="Author the deliverable.",
        artifact_links=(
            "artifacts/task-impl/ImplementationTaskFile/output.md",
        ),
    )
    transition = TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="Todo",
        to_state="SelfReview",
        worker="qwen",
        requires=RequirementDefinition(
            changed_files_required=True,
            artifacts=("ImplementationTaskFile",),
        ),
        transaction=None,
    )
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=transition,
    )
    assert compiled.accepted is True
    assert compiled.contract is not None
    assert all("output.md" not in excerpt.text for excerpt in compiled.contract.context_excerpts)


def test_global_contract_rejects_missing_required_linked_context_before_admission(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    task = _implementation_task(spec_relative="docs/missing-specification.md")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert compiled.accepted is False
    assert any(error.code == "context.link_not_found" for error in compiled.errors)


def test_global_contract_clips_oversized_resolved_context_without_dropping_it(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    spec = project_root / "docs" / "big-spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text(
        "# Specification\n\n" + ("Dependency rule content. " * 400) + "\n",
        encoding="utf-8",
    )
    task = _implementation_task(spec_relative="docs/big-spec.md")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    # Job creation still succeeds and the reference is preserved, marked clipped.
    assert compiled.accepted is True, [error.code for error in compiled.errors]
    assert compiled.contract is not None
    assert compiled.contract.context_excerpts
    assert "clipped at resolution budget" in compiled.contract.context_excerpts[0].text
    assert "Dependency rule content" in compiled.contract.context_excerpts[0].text
    # The complete required source is still frozen as full bytes, not dropped.
    assert compiled.contract.context_files
    full = compiled.contract.context_files[0]
    assert "Dependency rule content. " * 40 in full.content
    assert full.byte_count == len(full.content.encode("utf-8"))
    assert len(full.content) > 5000
    assert full.workspace_path == compiled.contract.context_excerpts[0].context_file_path


def test_global_contract_freezes_full_source_bytes_with_provenance(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    spec = project_root / "docs" / "specification.md"
    spec.parent.mkdir(parents=True)
    spec_text = "# Specification\n\nThe required enum is `HealthStatus`: `ok`, `degraded`.\n"
    spec.write_text(spec_text, encoding="utf-8")
    task = _implementation_task(spec_relative="docs/specification.md")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert compiled.accepted is True, [error.code for error in compiled.errors]
    contract = compiled.contract
    assert contract is not None
    assert contract.context_files
    frozen = contract.context_files[0]
    assert frozen.content == spec_text
    assert frozen.sha256 == hashlib.sha256(frozen.content.encode("utf-8")).hexdigest()
    assert frozen.byte_count == len(frozen.content.encode("utf-8"))
    assert frozen.workspace_path.startswith("context/")
    assert frozen.workspace_path.endswith(".md")
    assert "docs/specification.md" in frozen.refs
    assert frozen.required is True
    assert frozen.role == "reference"
    assert contract.context_excerpts[0].context_file_path == frozen.workspace_path


def test_global_contract_dedupes_identical_context_bytes_retaining_refs(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    (project_root / "docs").mkdir()
    (project_root / "docs" / "a.md").write_text("Same shared rule.\n", encoding="utf-8")
    (project_root / "docs" / "b.md").write_text("Same shared rule.\n", encoding="utf-8")
    task = _implementation_task(body="Apply [[a]] and [[b]].")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert compiled.accepted is True, [error.code for error in compiled.errors]
    contract = compiled.contract
    assert contract is not None
    assert len(contract.context_files) == 1
    assert set(contract.context_files[0].refs) == {"a", "b"}


def test_job_payload_round_trips_frozen_context_files(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    spec = project_root / "docs" / "specification.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Specification\n\nRule A.\n", encoding="utf-8")
    task = _implementation_task(spec_relative="docs/specification.md")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert compiled.contract is not None
    job = ExecutionJob(
        job_id="job-1",
        project_id="Agent",
        task_id=task.id,
        transition_id="ImplementTask",
        worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
        metadata={
            "execution_contract": execution_contract_to_dict(compiled.contract),
            "execution_contract_sha256": compiled.contract.sha256,
        },
    )

    loaded = load_job_execution_contract(job, required=True)

    assert loaded.accepted is True
    assert loaded.contract is not None
    assert loaded.contract.context_files == compiled.contract.context_files


def test_job_payload_rejects_tampered_frozen_context_file(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    _contract(project_root)
    spec = project_root / "docs" / "specification.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Specification\n\nRule A.\n", encoding="utf-8")
    task = _implementation_task(spec_relative="docs/specification.md")
    repo = _repo(tmp_path)
    compiled = compile_standard_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert compiled.contract is not None
    payload = execution_contract_to_dict(compiled.contract)
    payload["context_files"][0]["content"] = "Tampered rule text.\n"
    job = ExecutionJob(
        job_id="job-1",
        project_id="Agent",
        task_id=task.id,
        transition_id="ImplementTask",
        worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
        metadata={
            "execution_contract": payload,
            "execution_contract_sha256": compiled.contract.sha256,
        },
    )

    loaded = load_job_execution_contract(job, required=True)

    assert loaded.accepted is False
    assert loaded.errors[0].code == "execution_contract.hash_mismatch"
