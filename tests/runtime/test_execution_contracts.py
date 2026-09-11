from __future__ import annotations

from open_tulid.runtime.verification_runtime import HostCommandExecutor

import hashlib
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from open_tulid.domain import (
    ExecutionJob,
    RequirementDefinition,
    Task,
    TransitionDefinition,
    ValidationCallDefinition,
)
from open_tulid.runtime.execution_contracts import (
    FrozenContextExcerpt,
    FrozenContextFile,
    compile_task_execution_contract,
    execution_contract_to_dict,
    load_job_execution_contract,
)
from open_tulid.runtime.jobs import FileExecutionJobStore
from open_tulid.runtime.prompts import (
    PROMPT_COMPILER_VERSION,
    TOTAL_BUDGET,
    CompiledPrompt,
    PromptBudgetError,
    PromptManifest,
    PromptSection,
    ReviewEvidence,
    compile_execution_prompt,
    compiled_prompt_from_metadata,
    is_review_transition,
    lint_compiled_prompt,
)
from open_tulid.runtime.task_contracts import task_source_intent_sha256
from open_tulid.runtime.repository_facts import canonical_sha256, source_selection_to_dict
from open_tulid.runtime.workspaces import WorkspacePreparer
from open_tulid.runtime.verifier import CompletionSubmission, DeterministicVerifier


TASK_ID = "task-1"


def _task_and_contract(project_root: Path) -> Task:
    task = Task(
        id=TASK_ID,
        title="Add health",
        path="tasks/task-1.md",
        current_state="ReadyToImplement",
        task_type="ImplementationTask",
        metadata={"priority": "high"},
        body="Add a deterministic health endpoint.",
    )
    relative = Path(
        "artifacts/task-1/ImplementationContract/implementation-contract.yaml"
    )
    path = project_root / relative
    path.parent.mkdir(parents=True)
    path.write_text(
        f"""\
schema: tulid.implementation/v1
source:
  task_id: "{task.id}"
  source_intent_sha256: "{task_source_intent_sha256(task)}"
profile: code_change
objective: Add a deterministic health endpoint.
change_surface:
  add: []
  edit: [app.py]
  forbidden: [secrets/]
interfaces:
  - name: app.healthz
    behavior: Return ok.
requirements:
  - Preserve existing behavior.
failure_behavior: []
non_goals: []
checks:
  focused:
    - id: tests_pass
      argv: [python, check_repo.py, tests]
      timeout_seconds: 90
      expect:
        exit_code: 0
  invariants: [project_build]
""",
        encoding="utf-8",
    )
    return replace(task, artifact_links=(relative.as_posix(),))


def _transition(*, tests_command: str = "python check_repo.py tests"):
    return TransitionDefinition(
        id="ImplementTask",
        task_type="ImplementationTask",
        from_state="ReadyToImplement",
        to_state="SelfReview",
        worker="qwen",
        requires=RequirementDefinition(
            validations=(
                ValidationCallDefinition(
                    type="tests_pass",
                    args=MappingProxyType({"command": tests_command}),
                ),
                ValidationCallDefinition(
                    type="project_build",
                    args=MappingProxyType({
                        "command": ["python", "check_repo.py", "build"],
                    }),
                ),
            ),
            changed_files_required=True,
        ),
        transaction=None,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath("app.py").write_text(
        "def healthz():\n    return 'pending'\n",
        encoding="utf-8",
    )
    repo.joinpath("check_repo.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    return repo


def test_compile_freezes_task_transition_repository_and_resolved_checks(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    repo = _repo(tmp_path)

    first = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    second = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )

    assert first.accepted is True
    assert first.contract is not None
    assert second.contract is not None
    assert first.contract.sha256 == second.contract.sha256
    assert first.contract.source_task == task
    assert first.contract.repository_facts.repository_available is True
    assert [entry.path for entry in first.contract.baseline_manifest.entries] == [
        "app.py",
        "check_repo.py",
    ]
    assert [check.id for check in first.contract.resolved_checks] == [
        "project_build",
        "tests_pass",
    ]
    tests_check = first.contract.resolved_checks[1]
    assert tests_check.source == "task+transition"
    assert tests_check.argv == ("python", "check_repo.py", "tests")
    assert tests_check.timeout_seconds == 90


def test_compile_rejects_conflicting_task_and_transition_commands(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)

    result = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(tests_command="python check_repo.py all"),
    )

    assert result.accepted is False
    assert result.errors[0].code == "execution_contract.check_conflict"


def test_job_contract_round_trips_and_detects_tampering(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled.contract is not None
    payload = execution_contract_to_dict(compiled.contract)
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

    assert loaded.accepted is True
    assert loaded.contract is not None
    assert loaded.contract.source_task == task
    assert loaded.contract.transition == _transition()

    payload["source"]["task"]["body"] = "Silently broadened task."
    tampered = load_job_execution_contract(job, required=True)
    assert tampered.accepted is False
    assert tampered.errors[0].code == "execution_contract.hash_mismatch"


def test_git_repository_facts_freeze_source_selection_and_legacy_default(tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(("git", "-C", str(repo), "init", "-q"), check=False)
    subprocess.run(("git", "-C", str(repo), "config", "user.email", "t@e.com"), check=False)
    subprocess.run(("git", "-C", str(repo), "config", "user.name", "t"), check=False)
    node_modules = repo / "node_modules" / "project-owned"
    node_modules.mkdir(parents=True)
    (node_modules / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(repo), "add", "."), check=False)
    subprocess.run(("git", "-C", str(repo), "commit", "-qm", "init"), check=False)

    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert compiled.accepted is True, compiled.errors
    assert compiled.contract is not None
    facts = compiled.contract.repository_facts
    assert facts.git_repository is True
    assert facts.source_selection is not None
    assert facts.source_selection.mode == "git"
    assert "node_modules/project-owned/index.js" in facts.source_selection.tracked_paths

    # Round-trip through the frozen execution contract preserves the rule inputs.
    payload = execution_contract_to_dict(compiled.contract)
    job = ExecutionJob(
        job_id="job-git", project_id="Agent", task_id=task.id,
        transition_id="ImplementTask", worker_id="qwen",
        workspace_path=str(tmp_path / "ws"),
        metadata={"execution_contract": payload, "execution_contract_sha256": compiled.contract.sha256},
    )
    loaded = load_job_execution_contract(job, required=True)
    assert loaded.accepted is True and loaded.contract is not None
    loaded_selection = loaded.contract.repository_facts.source_selection
    assert loaded_selection is not None
    assert loaded_selection.mode == "git"
    assert source_selection_to_dict(loaded_selection)["tracked_paths"] == [
        "node_modules/project-owned/index.js",
    ]

    # A legacy serialized payload without the frozen field preserves its
    # historical interpretation: it loads with no source_selection rule, so old
    # contracts keep their old name-based behavior instead of an upgrade.
    for legacy_job in _payload_without_source_selection(task, tmp_path):
        loaded = load_job_execution_contract(legacy_job, required=True)
        assert loaded.accepted is True and loaded.contract is not None
        assert loaded.contract.repository_facts.source_selection is None


def _payload_without_source_selection(task, tmp_path):
    """Two modern git/non-git contracts stripped of the frozen source_selection
    field, re-hashed exactly like a historical record that predates R2."""
    repo = tmp_path / "legacy"
    repo.mkdir()
    (repo / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    compiled = compile_task_execution_contract(
        project_root=tmp_path / "project",
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert compiled.accepted is True and compiled.contract is not None
    payload = execution_contract_to_dict(compiled.contract)
    payload["repository"]["facts"].pop("source_selection", None)
    payload.pop("sha256")
    expected = canonical_sha256(payload)
    payload["sha256"] = expected
    yield ExecutionJob(
        job_id="job-legacy", project_id="Agent", task_id=task.id,
        transition_id="ImplementTask", worker_id="qwen",
        workspace_path=str(tmp_path / "ws"),
        metadata={"execution_contract": payload, "execution_contract_sha256": expected},
    )


def test_compile_rejects_conflicting_task_and_transition_commands(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled.contract is not None
    payload = execution_contract_to_dict(compiled.contract)
    payload.pop("sha256")
    payload["prompt_compiler_version"] = 1
    expected_hash = canonical_sha256(payload)
    payload["sha256"] = expected_hash
    job = ExecutionJob(
        job_id="job-v1",
        project_id="Agent",
        task_id=task.id,
        transition_id="ImplementTask",
        worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
        metadata={
            "execution_contract": payload,
            "execution_contract_sha256": expected_hash,
        },
    )

    loaded = load_job_execution_contract(job, required=True)

    assert loaded.accepted is True
    assert loaded.contract is not None
    assert loaded.contract.sha256 == expected_hash


def test_job_store_rejects_frozen_contract_replacement(tmp_path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    created = store.create(ExecutionJob(
        job_id="job-1",
        project_id="Agent",
        task_id=TASK_ID,
        transition_id="ImplementTask",
        worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
        metadata={
            "execution_contract": {"schema": "original"},
            "execution_contract_sha256": "a" * 64,
        },
    ))
    assert created.accepted is True

    updated = store.update_status(
        "job-1",
        "running",
        metadata={"execution_contract_sha256": "b" * 64},
    )

    assert updated.accepted is False
    assert updated.error is not None
    assert updated.error.code == "job.immutable_metadata"


def test_workspace_writes_frozen_contract_files_and_rejects_repo_drift(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    repo = _repo(tmp_path)
    transition = _transition()
    compiled = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=transition,
    )
    assert compiled.contract is not None
    job = ExecutionJob(
        job_id="job-1",
        project_id="Agent",
        task_id=task.id,
        transition_id=transition.id,
        worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
        metadata={
            "execution_contract": execution_contract_to_dict(compiled.contract),
            "execution_contract_sha256": compiled.contract.sha256,
        },
    )

    prepared = WorkspacePreparer(repo_root=repo).prepare(
        job=job,
        task=task,
        transition=transition,
    )

    assert prepared.accepted is True
    context_root = Path(job.workspace_path) / ".open-tulid"
    assert context_root.joinpath("execution-contract.json").is_file()
    assert context_root.joinpath("repository-facts.json").is_file()
    assert context_root.joinpath("baseline-manifest.json").is_file()

    drifted_workspace = tmp_path / "drifted-workspace"
    drifted_job = replace(job, workspace_path=str(drifted_workspace))
    repo.joinpath("app.py").write_text("changed after scheduling\n", encoding="utf-8")

    drifted = WorkspacePreparer(repo_root=repo).prepare(
        job=drifted_job,
        task=task,
        transition=transition,
    )

    assert drifted.accepted is False
    assert drifted.error is not None
    assert drifted.error.code == "workspace.baseline_mismatch"


def test_compiled_prompt_is_deterministic_compact_and_has_single_completion_example(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled.contract is not None

    first = compile_execution_prompt(compiled.contract)
    second = compile_execution_prompt(compiled.contract)

    assert first.text == second.text
    assert first.manifest.packet_sha256 == second.manifest.packet_sha256
    assert [section.heading for section in first.sections] == [
        "Assigned Task", "Required Reading", "Repository Facts",
        "Execution Procedure", "Required Validation", "Completion Submission",
    ]
    assert len(first.text) <= TOTAL_BUDGET
    assert first.text.count("curl -sS -X POST") == 1
    assert first.text.count("```sh") == 1
    assert str(project_root) not in first.text
    assert compiled.contract.sha256 not in first.text
    assert first.manifest.execution_contract_sha256 == compiled.contract.sha256
    assert first.manifest.packet_type == "implementation"
    assert lint_compiled_prompt(first, contract=compiled.contract) == ()


def test_im2c_prompt_renders_one_authoritative_task_and_exact_reading_paths(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    spec_content = "# Spec\nRequired enum E {A, B}.\n" + ("x" * 20)
    spec_sha = hashlib.sha256(spec_content.encode("utf-8")).hexdigest()
    context_file = FrozenContextFile(
        workspace_path=f"context/{spec_sha[:12]}.md",
        content=spec_content,
        sha256=spec_sha,
        byte_count=len(spec_content.encode("utf-8")),
        required=True,
        reason="Defines the required enum and serialization rules.",
        role="reference",
        refs=("artifacts/task-1/spec.md",),
    )
    excerpt_text = "# Linked Reference Context: artifacts/task-1/spec.md\n\n" + spec_content
    excerpt = FrozenContextExcerpt(
        artifact="artifacts/task-1/spec.md",
        heading="Linked Reference Context",
        reason="Defines the required enum and serialization rules.",
        text=excerpt_text,
        sha256=hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest(),
        context_file_path=context_file.workspace_path,
    )
    contract = replace(
        compiled_contract.contract,
        context_files=(context_file,),
        context_excerpts=(excerpt,),
    )

    prompt = compile_execution_prompt(contract)

    assert [section.heading for section in prompt.sections] == [
        "Assigned Task", "Required Reading", "Repository Facts",
        "Execution Procedure", "Required Validation", "Completion Submission",
    ]
    # The task body appears exactly once, in the authoritative Assigned Task
    # section, and never under a synthetic execution-contract objective.
    task_body = contract.source_task.body.strip()
    assert prompt.text.count(task_body) == 1
    assert [s.id for s in prompt.sections].count("assigned_task") == 1
    assert "## Execution Contract" not in prompt.text
    reading = next(
        section for section in prompt.sections if section.id == "required_reading"
    )
    assert f".open-tulid/{context_file.workspace_path}" in reading.text
    assert "required reference: Defines the required enum and serialization rules." in reading.text
    assert lint_compiled_prompt(prompt, contract=contract) == ()


def test_historical_prompt_round_trips_and_rejects_section_tampering(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    prompt = compile_execution_prompt(compiled_contract.contract)
    metadata = {
        "prompt_packet": prompt.text,
        "prompt_packet_sha256": prompt.manifest.packet_sha256,
        "prompt_manifest": prompt.manifest.to_dict(),
    }

    loaded = compiled_prompt_from_metadata(metadata)

    assert loaded.text == prompt.text
    assert loaded.sections == prompt.sections

    tampered_manifest = prompt.manifest.to_dict()
    tampered_manifest["sections"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="section failed"):
        compiled_prompt_from_metadata({
            **metadata,
            "prompt_manifest": tampered_manifest,
        })

    malformed_manifest = prompt.manifest.to_dict()
    malformed_manifest["compiler_version"] = {}
    with pytest.raises(ValueError, match="compiler_version"):
        compiled_prompt_from_metadata({
            **metadata,
            "prompt_manifest": malformed_manifest,
        })


def test_historical_prompt_round_trip_treats_nested_markdown_headings_as_content(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    excerpt_text = "# Selected API\n\nKeep this contract.\n\n## Nested Detail\n\nDo not split here."
    excerpt = FrozenContextExcerpt(
        artifact="design.md",
        heading="Selected API",
        reason="Defines the selected interface.",
        text=excerpt_text,
        sha256=hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest(),
    )
    contract = replace(
        compiled_contract.contract,
        context_excerpts=(excerpt,),
    )
    prompt = compile_execution_prompt(contract)

    loaded = compiled_prompt_from_metadata({
        "prompt_packet": prompt.text,
        "prompt_packet_sha256": prompt.manifest.packet_sha256,
        "prompt_manifest": prompt.manifest.to_dict(),
    })

    selected = next(
        section for section in loaded.sections
        if section.id == "required_reading"
    )
    assert "## Nested Detail" in selected.text
    assert selected.truncated is False
    assert loaded.text == prompt.text


def test_prompt_does_not_truncate_binding_task_then_requires_reading(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    body = "Deliver exact behavior " + ("carefully " * 119) + "exactly."
    body_stripped = body.strip()
    excerpt_text = (
        "# Required Detail\n\n" + ("binding reference text " * 50)
    ).rstrip()
    excerpt = FrozenContextExcerpt(
        artifact="design.md",
        heading="Required Detail",
        reason="Defines required behavior.",
        text=excerpt_text,
        sha256=hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest(),
    )
    contract = replace(
        compiled_contract.contract,
        source_task=replace(
            compiled_contract.contract.source_task,
            body=body,
        ),
        context_excerpts=(excerpt,),
    )

    prompt = compile_execution_prompt(contract)

    task_section = next(
        section for section in prompt.sections if section.id == "assigned_task"
    )
    reading_section = next(
        section for section in prompt.sections if section.id == "required_reading"
    )
    assert body_stripped in task_section.text
    assert task_section.truncated is False
    assert excerpt_text in reading_section.text
    assert reading_section.truncated is False


def test_prompt_allows_user_content_that_looks_like_an_unrelated_sha256(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    user_hash = "d" * 64
    contract = replace(
        compiled_contract.contract,
        source_task=replace(
            compiled_contract.contract.source_task,
            body=(
                f"Preserve the documented content id {user_hash} and the "
                "literal example {{checksum}}."
            ),
        ),
    )

    prompt = compile_execution_prompt(contract)

    assert user_hash in prompt.text
    assert "{{checksum}}" in prompt.text
    assert lint_compiled_prompt(prompt, contract=contract) == ()


def test_review_transition_detection_uses_tokens_not_substrings():
    assert is_review_transition(replace(_transition(), id="SelfReview")) is True
    assert is_review_transition(replace(_transition(), id="PreviewChanges")) is False
    assert is_review_transition(replace(_transition(), id="Audit", from_state="Checking", review=True))
    assert not is_review_transition(replace(_transition(), id="SelfReview", review=False))


def test_self_review_prompt_is_distinct_and_uses_prior_authoritative_evidence(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    review_transition = replace(
        _transition(),
        id="SelfReview",
        from_state="SelfReview",
        to_state="Done",
        requires=replace(
            _transition().requires,
            changed_files_required=False,
        ),
    )
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=review_transition,
    )
    assert compiled_contract.contract is not None
    evidence = ReviewEvidence(
        source_job_id="implementation-job",
        verification_report={
            "changes": {
                "added": [],
                "edited": ["app.py"],
                "removed": [],
                "renamed": [],
                "changed_lines": 2,
            },
            "checks": [{"id": "tests_pass", "status": "passed", "exit_code": 0}],
        },
    )

    prompt = compile_execution_prompt(
        compiled_contract.contract,
        review_evidence=evidence,
    )

    assert prompt.manifest.packet_type == "self_review"
    assert "## Prior Implementation Evidence" in prompt.text
    assert "Source implementation job: implementation-job" in prompt.text
    assert '"edited":["app.py"]' in prompt.text
    assert "Find a concrete in-scope defect" in prompt.text
    assert "make the smallest coherent implementation" not in prompt.text.casefold()
    assert prompt.text.count("curl -sS -X POST") == 1


def test_review_packet_is_requirement_driven_and_carries_evidence_records(tmp_path):
    """Plan 6B: the review packet is requirement-driven and consumes the frozen
    task, prior verification/change evidence, and bounded repair history."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    review_transition = replace(
        _transition(),
        id="SelfReview",
        from_state="SelfReview",
        to_state="Done",
        requires=replace(
            _transition().requires,
            changed_files_required=False,
        ),
    )
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=review_transition,
    )
    assert compiled_contract.contract is not None
    evidence = ReviewEvidence(
        source_job_id="implementation-job",
        verification_report={
            "changes": {"added": [], "edited": ["app.py"], "removed": [], "renamed": [], "changed_lines": 2},
            "checks": [{"id": "tests_pass", "status": "passed", "exit_code": 0}],
        },
        repair_history=(
            {
                "classification": "implementation_failure",
                "error_codes": ("completion.artifact_missing",),
                "repair_ready": True,
                "retry_reason": "artifact missing",
            },
        ),
    )

    prompt = compile_execution_prompt(compiled_contract.contract, review_evidence=evidence)

    # 1. The authoritative task body and requirements are present.
    assert "## Assigned Task and Requirements" in prompt.text
    assert "## Required Reading" in prompt.text
    # 2. The procedure is requirement-to-evidence driven.
    procedure = next(s.text for s in prompt.sections if s.id == "review_procedure")
    assert "map it to code and tests" in procedure
    assert "integration seams" in procedure
    assert "missing behavior" in procedure
    assert "scope drift" in procedure
    assert "unfinished user-facing states" in procedure
    # 3. Authoritative prior evidence and bounded repair history are retained.
    evidence_section = next(s.text for s in prompt.sections if s.id == "prior_implementation_evidence")
    assert "Authoritative changes:" in evidence_section
    assert "repair_ready" in evidence_section or "repair" in evidence_section.casefold()
    # 4. A compact requirement-to-evidence review result is requested and kept in the record.
    assert "review_result" in prompt.text
    assert "remaining_blockers" in prompt.text
    guidance = next(s.text for s in prompt.sections if s.id == "review_result")
    assert "clarification/planning path" in guidance
    assert prompt.text.count("curl -sS -X POST") == 1


def test_context_excerpt_rejects_duplicate_heading_and_oversized_content(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    spec_relative = Path("artifacts/task-1/ImplementationSpec/spec.md")
    spec = project_root / spec_relative
    spec.parent.mkdir(parents=True)
    spec.write_text("# Needed\nfirst\n# Needed\nsecond\n", encoding="utf-8")
    contract_path = project_root / task.artifact_links[0]
    contract_path.write_text(
        contract_path.read_text(encoding="utf-8")
        + "\ncontext_excerpts:\n"
        + "  - artifact: ImplementationSpec\n"
        + "    heading: Needed\n"
        + "    reason: Exact interface behavior.\n",
        encoding="utf-8",
    )
    task = replace(task, artifact_links=(*task.artifact_links, spec_relative.as_posix()))

    repo = _repo(tmp_path)
    duplicate = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )

    assert [error.code for error in duplicate.errors] == [
        "execution_contract.context_heading_duplicate"
    ]

    spec.write_text("# Needed\n" + ("x" * 1201) + "\n", encoding="utf-8")
    oversized = compile_task_execution_contract(
        project_root=project_root,
        repo_root=repo,
        task=task,
        transition=_transition(),
    )
    assert [error.code for error in oversized.errors] == [
        "execution_contract.context_excerpt_too_large"
    ]


def test_verifier_runs_frozen_checks_without_path_diff_rejection(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    contract_path = project_root / task.artifact_links[0]
    contract_path.write_text(contract_path.read_text(encoding="utf-8").replace("invariants: [project_build]", "invariants: []"), encoding="utf-8")
    repo = _repo(tmp_path)
    transition = replace(_transition(), requires=RequirementDefinition(changed_files_required=True))
    compiled = compile_task_execution_contract(
        project_root=project_root, repo_root=repo, task=task, transition=transition,
    )
    assert compiled.contract is not None
    repo.joinpath("app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")

    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=transition,
        submission=CompletionSubmission(changed_files=("app.py",)),
        execution_contract=compiled.contract,
    )

    assert result.accepted is True
    assert result.report is not None
    # File diffs are recorded as history only; the report carries the command
    # checks as the acceptance evidence.
    assert [check.status for check in result.report.checks] == ["passed"]


def test_verifier_allows_unknown_file_without_contract_rejection(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    contract_path = project_root / task.artifact_links[0]
    contract_path.write_text(contract_path.read_text(encoding="utf-8").replace("invariants: [project_build]", "invariants: []"), encoding="utf-8")
    repo = _repo(tmp_path)
    transition = replace(_transition(), requires=RequirementDefinition(changed_files_required=True))
    compiled = compile_task_execution_contract(
        project_root=project_root, repo_root=repo, task=task, transition=transition,
    )
    assert compiled.contract is not None
    # The worker legitimately creates a previously unknown file. It must not be
    # rejected merely because it was not predicted in the baseline.
    (repo / "research").mkdir()
    (repo / "research" / "notes.md").write_text("# Plan\n", encoding="utf-8")

    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo,
        transition=transition,
        submission=CompletionSubmission(changed_files=("research/notes.md",)),
        execution_contract=compiled.contract,
    )

    assert result.accepted is True
    assert result.report is not None
    assert all(check.status == "passed" for check in result.report.checks)


def test_full_task_exceeding_budget_fails_with_named_budget_error(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    oversized = replace(
        compiled_contract.contract,
        source_task=replace(
            compiled_contract.contract.source_task,
            body="A" * 8000,
        ),
    )

    with pytest.raises(PromptBudgetError) as excinfo:
        compile_execution_prompt(oversized)

    assert excinfo.value.code == "prompt.budget_exceeded"
    assert "exceed" in str(excinfo.value)


def test_inline_excerpts_are_trimmed_before_binding_overflow(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    body = "Deliver exact behavior " + ("carefully " * 290) + "exactly."
    excerpt_text = "# Required Detail\n\n" + ("binding reference text " * 60)
    spec_sha = hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest()
    context_file = FrozenContextFile(
        workspace_path=f"context/{spec_sha[:12]}.md",
        content=excerpt_text,
        sha256=spec_sha,
        byte_count=len(excerpt_text.encode("utf-8")),
        required=True,
        reason="Defines required behavior.",
        role="reference",
        refs=("artifacts/task-1/spec.md",),
    )
    excerpt = FrozenContextExcerpt(
        artifact="artifacts/task-1/spec.md",
        heading="Required Detail",
        reason="Defines required behavior.",
        text=excerpt_text,
        sha256=hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest(),
        context_file_path=context_file.workspace_path,
    )
    contract = replace(
        compiled_contract.contract,
        source_task=replace(compiled_contract.contract.source_task, body=body),
        context_files=(context_file,),
        context_excerpts=(excerpt,),
    )

    prompt = compile_execution_prompt(contract)

    assert len(prompt.text) <= TOTAL_BUDGET
    assert body.strip() in prompt.text
    task_section = next(
        section for section in prompt.sections if section.id == "assigned_task"
    )
    assert task_section.truncated is False
    reading = next(
        section for section in prompt.sections if section.id == "required_reading"
    )
    assert f".open-tulid/{context_file.workspace_path}" in reading.text
    assert "[additional context excerpts omitted" in reading.text
    assert excerpt_text not in reading.text
    assert prompt.manifest.optional_omissions
    assert lint_compiled_prompt(prompt, contract=contract) == ()


def test_prompt_lint_rejects_duplicate_authoritative_task_content(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    body = "Add a deterministic health endpoint."
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    excerpt = FrozenContextExcerpt(
        artifact="spec.md",
        heading="Spec",
        reason="Duplicated task body.",
        text=body,
        sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
    contract = replace(compiled_contract.contract, context_excerpts=(excerpt,))

    with pytest.raises(ValueError, match="prompt.duplicate_task_content"):
        compile_execution_prompt(contract)


def test_prompt_lint_rejects_forbidden_task_local_command_block(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    excerpt_text = (
        "# Required Detail\n\n```sh\nrun my own command\n```\n"
    )
    excerpt = FrozenContextExcerpt(
        artifact="design.md",
        heading="Required Detail",
        reason="Contains a forbidden command block.",
        text=excerpt_text,
        sha256=hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest(),
    )
    contract = replace(compiled_contract.contract, context_excerpts=(excerpt,))

    with pytest.raises(ValueError, match="prompt.forbidden_command_block"):
        compile_execution_prompt(contract)


def _manual_compiled(sections, contract):
    rendered = "\n\n".join(f"## {section.heading}\n\n{section.text}" for section in sections)
    packet_sha = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    manifest_sections = tuple({
        "id": section.id,
        "heading": section.heading,
        "source_kind": section.source_kind,
        "source_ref": section.source_ref,
        "selection_reason": section.selection_reason,
        "sha256": hashlib.sha256(section.text.encode("utf-8")).hexdigest(),
        "characters": len(section.text),
        "budget": section.budget,
        "truncated": section.truncated,
    } for section in sections)
    manifest = PromptManifest(
        PROMPT_COMPILER_VERSION,
        "implementation",
        contract.sha256,
        manifest_sections,
        packet_sha,
        len(rendered),
        TOTAL_BUDGET,
        (),
    )
    return CompiledPrompt(rendered, tuple(sections), manifest)


def test_prompt_lint_rejects_unresolved_and_missing_reading_paths(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    content = "Required enum E {A, B}.\n"
    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    context_file = FrozenContextFile(
        workspace_path=f"context/{sha[:12]}.md",
        content=content,
        sha256=sha,
        byte_count=len(content.encode("utf-8")),
        required=True,
        reason="Defines the required enum.",
        role="reference",
        refs=("artifacts/task-1/spec.md",),
    )
    contract = replace(compiled_contract.contract, context_files=(context_file,))
    missing = next(f.workspace_path for f in contract.context_files)
    unresolved = "context/deadbeefdead.md"
    reading = "\n".join((
        "Read the complete frozen source files below.",
        f"- `.open-tulid/{unresolved}` — required reference: unresolved",
    ))
    compiled = _manual_compiled((
        PromptSection(
            "assigned_task", "Assigned Task", task.body,
            "execution_contract", "generated_contract", "Assigned outcome.",
        ),
        PromptSection(
            "required_reading", "Required Reading", reading,
            "context_file", unresolved, "Required reading.",
        ),
        PromptSection(
            "completion_submission", "Completion Submission",
            "```sh\ncurl -sS -X POST\n```",
            "runtime", "completion_api", "Completion mechanism.",
        ),
    ), contract)

    issues = lint_compiled_prompt(compiled, contract=contract)
    codes = {issue.code for issue in issues}
    assert "prompt.missing_required_source" in codes
    assert "prompt.unresolved_reading_path" in codes


def test_prompt_manifest_optional_omissions_round_trip(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    compiled_contract = compile_task_execution_contract(
        project_root=project_root,
        repo_root=_repo(tmp_path),
        task=task,
        transition=_transition(),
    )
    assert compiled_contract.contract is not None
    excerpt_text = "# Required Detail\n\n" + ("binding reference text " * 60)
    sha = hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest()
    context_file = FrozenContextFile(
        workspace_path=f"context/{sha[:12]}.md",
        content=excerpt_text,
        sha256=sha,
        byte_count=len(excerpt_text.encode("utf-8")),
        required=True,
        reason="Defines required behavior.",
        role="reference",
        refs=("artifacts/task-1/spec.md",),
    )
    excerpt = FrozenContextExcerpt(
        artifact="artifacts/task-1/spec.md",
        heading="Required Detail",
        reason="Defines required behavior.",
        text=excerpt_text,
        sha256=hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest(),
        context_file_path=context_file.workspace_path,
    )
    base = replace(compiled_contract.contract, context_files=(context_file,))
    contract = replace(
        base,
        source_task=replace(base.source_task, body="Deliver exact behavior " + ("carefully " * 290) + "exactly."),
        context_excerpts=(excerpt,),
    )
    prompt = compile_execution_prompt(contract)

    assert prompt.manifest.optional_omissions
    loaded = compiled_prompt_from_metadata({
        "prompt_packet": prompt.text,
        "prompt_packet_sha256": prompt.manifest.packet_sha256,
        "prompt_manifest": prompt.manifest.to_dict(),
    })

    assert loaded.manifest.optional_omissions == prompt.manifest.optional_omissions


def test_verifier_rejects_when_a_frozen_command_fails(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    task = _task_and_contract(project_root)
    contract_path = project_root / task.artifact_links[0]
    contract_path.write_text(contract_path.read_text(encoding="utf-8").replace("invariants: [project_build]", "invariants: []"), encoding="utf-8")
    repo = _repo(tmp_path)
    transition = replace(_transition(), requires=RequirementDefinition(changed_files_required=True))
    compiled = compile_task_execution_contract(
        project_root=project_root, repo_root=repo, task=task, transition=transition,
    )
    assert compiled.contract is not None
    # Break the focused check command so the verification command exits non-zero.
    repo.joinpath("check_repo.py").write_text("raise SystemExit(1)\n", encoding="utf-8")

    result = DeterministicVerifier(executor=HostCommandExecutor()).verify(
        workspace=repo, transition=transition,
        submission=CompletionSubmission(changed_files=("app.py",)), execution_contract=compiled.contract,
    )

    assert result.accepted is False
    assert {error.code for error in result.errors} == {"verification.check_failed"}


def test_job_store_freezes_verification_environment(tmp_path):
    store = FileExecutionJobStore(tmp_path / "jobs")
    assert store.create(ExecutionJob(
        job_id="job-env", project_id="Agent", task_id=TASK_ID,
        transition_id="ImplementTask", worker_id="qwen",
        workspace_path=str(tmp_path / "workspace"),
    )).accepted
    assert store.update_status("job-env", "running", metadata={
        "verification_environment": {"project_image_identity": "sha256:original"},
    }).accepted
    changed = store.update_status("job-env", "running", metadata={
        "verification_environment": {"project_image_identity": "sha256:changed"},
    })
    assert not changed.accepted
    assert changed.error.code == "job.immutable_metadata"
