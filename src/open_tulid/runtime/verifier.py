from __future__ import annotations

import fnmatch
import hashlib
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from open_tulid.domain import DomainError, TransitionDefinition
from open_tulid.runtime.execution_contracts import ExecutionContract
from open_tulid.runtime.repository_facts import (
    BaselineManifest,
    canonical_sha256,
    capture_repository_snapshot,
)

# Legacy schema name, retained so historical ``tulid.verification/v1`` reports
# stay readable. New verification reports use v2 and keep every v1 field.
VERIFICATION_REPORT_SCHEMA = "tulid.verification/v1"
VERIFICATION_REPORT_SCHEMA_V2 = "tulid.verification/v2"
VERIFICATION_REQUEST_SCHEMA = "tulid.verification_request/v1"

# Granular classification recorded separately from pass/fail (plan 4A). A
# command that was never run, or an empty report, is never equated with success.
VERIFICATION_NOT_RUN_STATUS = "not_run"
VERIFICATION_PASSED = "passed"
VERIFICATION_CLASSIFICATION_PASSED = "passed"
VERIFICATION_CLASSIFICATION_MALFORMED_POLICY = "malformed_policy"
VERIFICATION_CLASSIFICATION_ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
VERIFICATION_CLASSIFICATION_DEPENDENCY_PREPARATION = "dependency_preparation"
VERIFICATION_CLASSIFICATION_COMMAND_TIMEOUT = "command_timeout"
VERIFICATION_CLASSIFICATION_FAILED_BEHAVIOR = "failed_behavior"
VERIFICATION_CLASSIFICATION_SOURCE_MUTATION = "source_mutation"
VERIFICATION_CLASSIFICATION_INFRASTRUCTURE_INTERRUPTION = "infrastructure_interruption"

# Inline stdout/stderr in a report is bounded; complete logs are retained as
# referenced artifacts rather than embedded wholesale into the report.
LOG_EXCERPT_CHARACTER_LIMIT = 2_000


@dataclass(frozen=True)
class ArtifactSubmission:
    type: str
    path: str
    sha256: str | None = None


REVIEW_RESULT_SCHEMA = "tulid.review_result/v1"


@dataclass(frozen=True)
class CompletionSubmission:
    submission_id: str | None = None
    attempt: int | None = None
    summary: str = ""
    artifacts: tuple[ArtifactSubmission, ...] = ()
    changed_files: tuple[str, ...] = ()
    validation_evidence: Mapping[str, str] = field(default_factory=dict)
    # Compact requirement-to-evidence result required for review transitions
    # (plan 6B). It names the behavior, cited source/test evidence, defects/fixes,
    # and remaining blockers. Retained in the completion/acceptance record.
    review_result: Mapping[str, object] | None = None


@dataclass(frozen=True)
class VerificationResult:
    accepted: bool
    errors: tuple[DomainError, ...] = ()
    report: "VerificationReport | None" = None

    @property
    def message(self) -> str:
        if self.accepted:
            return "Completion accepted."
        return "; ".join(error.message for error in self.errors)


@dataclass(frozen=True)
class VerificationCheckResult:
    """One configured global command and its verifier result.

    ``status`` is one of ``passed``/``failed``/``timeout``/``environment_error``
    or ``not_run``. A check marked ``not_run`` means the command never executed
    (for example verification was interrupted), so it can never count as success.
    Inline ``stdout``/``stderr`` excerpts are bounded; complete command logs are
    referenced by ``log_refs`` rather than embedded wholesale.
    """

    id: str
    status: str
    argv: tuple[str, ...]
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    working_directory: str = "."
    timeout_seconds: int | None = None
    expected_exit_code: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_seconds: float | None = None
    log_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "working_directory": self.working_directory,
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_code": self.expected_exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "log_refs": list(self.log_refs),
        }


@dataclass(frozen=True)
class CoverageChange:
    """A baseline-to-candidate change to a coverage-relevant path (plan 4E).

    Coverage-relevant paths are test files, test-discovery/build configuration,
    and suites whose composition can change without a later command failing.
    These changes are recorded on the report so review can see whether a suite
    was disabled to make a global command exit zero. Legitimate test refactors
    remain allowed; semantic test-quality assessment stays with review (plan 6),
    so the verifier only surfaces the fact, it never blocks solely on it.
    """

    kind: str
    path: str
    before_sha256: str | None = None
    after_sha256: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "path": self.path,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
        }


@dataclass(frozen=True)
class VerificationReport:
    """Tulid-controlled verification evidence binding checks to one candidate.

    ``classification`` is the granular outcome recorded separately from the
    accepted/passed boolean; it distinguishes a malformed policy, an unavailable
    tool/runtime, a dependency preparation failure, a command timeout, a failed
    behavior check, source mutation, or an infrastructure interruption. An empty
    report or any ``not_run`` check is never equated with success.
    """

    schema: str
    classification: str | None
    baseline_sha256: str
    post_manifest_sha256: str | None
    added: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()
    changed_lines: int = 0
    checks: tuple[VerificationCheckResult, ...] = ()
    # Identity of the sealed candidate this report verifies (plan 5, step 5B).
    # A report is applicable only to its exact sealed candidate.
    candidate_id: str | None = None
    candidate_manifest_sha256: str | None = None
    # Plan 4A: verifier request identity and frozen policy identity.
    request_sha256: str | None = None
    command_policy_sha256: str | None = None
    project_image_identity: str | None = None
    environment_identity: str | None = None
    not_run_checks: int = 0
    source_mutated: bool | None = None
    # Granular outcome recorded separately from the coarse/legacy classification
    # and from the acceptance boolean (plan 4A): malformed policy, unavailable
    # tool/runtime, dependency preparation, command timeout, failed behavior,
    # source mutation, or infrastructure interruption.
    classification_detail: str | None = None
    # Baseline-to-candidate changes on coverage-relevant paths, surfaced for
    # review so a disabled test suite cannot be accepted merely because a
    # global command exits zero (plan 4E).
    coverage_changes: tuple[CoverageChange, ...] = ()

    @property
    def accepted_inline(self) -> bool:
        """Inline sanity: an empty report or any never-run command is never success."""
        if not self.checks:
            return False
        if self.not_run_checks:
            return False
        return all(check.status == VERIFICATION_PASSED for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "classification": self.classification,
            "baseline_sha256": self.baseline_sha256,
            "post_manifest_sha256": self.post_manifest_sha256,
            "changes": {"added": list(self.added), "edited": list(self.edited),
                        "removed": list(self.removed),
                        "renamed": [{"from": old, "to": new} for old, new in self.renamed],
                        "changed_lines": self.changed_lines},
            "checks": [check.to_dict() for check in self.checks],
            "candidate_id": self.candidate_id,
            "candidate_manifest_sha256": self.candidate_manifest_sha256,
            "request_sha256": self.request_sha256,
            "command_policy_sha256": self.command_policy_sha256,
            "project_image_identity": self.project_image_identity,
            "environment_identity": self.environment_identity,
            "not_run_checks": self.not_run_checks,
            "source_mutated": self.source_mutated,
            "classification_detail": self.classification_detail,
            "coverage_changes": [change.to_dict() for change in self.coverage_changes],
        }


@dataclass(frozen=True)
class VerificationCommand:
    """One ordered global command definition frozen into a verifier request."""

    name: str
    argv: tuple[str, ...]
    working_directory: str = "."
    timeout_seconds: int = 300
    expected_exit_code: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "working_directory": self.working_directory,
            "timeout_seconds": self.timeout_seconds,
            "expected_exit_code": self.expected_exit_code,
        }


@dataclass(frozen=True)
class VerificationRequest:
    """What this verifier was asked to execute against which exact candidate.

    The request freezes the ordered command policy (never re-sorted), the
    command-policy digest, the candidate and baseline digests, the resolved
    project image identity, and the runtime environment identity. The report is
    bound to this request's digest.
    """

    schema: str = VERIFICATION_REQUEST_SCHEMA
    candidate_id: str | None = None
    candidate_manifest_sha256: str | None = None
    baseline_sha256: str | None = None
    command_policy_sha256: str | None = None
    project_image_identity: str | None = None
    environment_identity: str | None = None
    commands: tuple[VerificationCommand, ...] = ()
    sha256: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "candidate_id": self.candidate_id,
            "candidate_manifest_sha256": self.candidate_manifest_sha256,
            "baseline_sha256": self.baseline_sha256,
            "command_policy_sha256": self.command_policy_sha256,
            "project_image_identity": self.project_image_identity,
            "environment_identity": self.environment_identity,
            "commands": [command.to_dict() for command in self.commands],
            "sha256": self.sha256,
        }


def command_policy_sha256(commands: Sequence[VerificationCommand]) -> str:
    """Canonical digest of an ordered global command policy.

    The order is part of the identity: relisting ``a_tests`` before
    ``z_setup`` is a different policy from the reverse, so a verifier consumes
    the exact declared order rather than silently sorting it.
    """
    body = {
        "schema": "tulid.command_policy/v1",
        "commands": [
            {
                "name": command.name,
                "argv": list(command.argv),
                "working_directory": command.working_directory,
                "timeout_seconds": command.timeout_seconds,
                "expected_exit_code": command.expected_exit_code,
            }
            for command in commands
        ],
    }
    return canonical_sha256(body)


def verification_request_from_execution_contract(
    contract: ExecutionContract,
    *,
    candidate_id: str | None = None,
    candidate_manifest_sha256: str | None = None,
    project_image_identity: str | None = None,
    environment_identity: str | None = None,
) -> VerificationRequest:
    """Freeze a verifier request from a compiled execution contract.

    The ordered command list is taken from the resolved global checks in their
    declared order, never re-sorted. ``project_image_identity`` and
    ``environment_identity`` are resolved by the external execution environment
    (plan 4B); callers that do not yet resolve them leave them ``None``.
    """
    commands: list[VerificationCommand] = []
    for check in contract.resolved_checks:
        if check.runner != "command":
            continue
        commands.append(VerificationCommand(
            name=check.id,
            argv=check.argv,
            working_directory=check.working_directory,
            timeout_seconds=check.timeout_seconds,
            expected_exit_code=check.expect.exit_code,
        ))
    ordered = tuple(commands)
    policy_digest = command_policy_sha256(ordered)
    provisional = VerificationRequest(
        schema=VERIFICATION_REQUEST_SCHEMA,
        candidate_id=candidate_id,
        candidate_manifest_sha256=candidate_manifest_sha256,
        baseline_sha256=contract.baseline_manifest.sha256,
        command_policy_sha256=policy_digest,
        project_image_identity=project_image_identity,
        environment_identity=environment_identity,
        commands=ordered,
        sha256="",
    )
    request_hash = canonical_sha256(_verification_request_body(provisional))
    return VerificationRequest(
        candidate_id=provisional.candidate_id,
        candidate_manifest_sha256=provisional.candidate_manifest_sha256,
        baseline_sha256=provisional.baseline_sha256,
        command_policy_sha256=provisional.command_policy_sha256,
        project_image_identity=provisional.project_image_identity,
        environment_identity=provisional.environment_identity,
        commands=provisional.commands,
        sha256=request_hash,
    )


def _verification_request_body(request: VerificationRequest) -> dict[str, object]:
    return {
        "schema": request.schema,
        "candidate_id": request.candidate_id,
        "candidate_manifest_sha256": request.candidate_manifest_sha256,
        "baseline_sha256": request.baseline_sha256,
        "command_policy_sha256": request.command_policy_sha256,
        "project_image_identity": request.project_image_identity,
        "environment_identity": request.environment_identity,
        "commands": [command.to_dict() for command in request.commands],
    }


class DeterministicVerifier:
    def __init__(
        self,
        *,
        artifact_templates: Mapping[str, str | None] | None = None,
        validation_implementations: Mapping[str, Callable[..., object]] | None = None,
        validation_context_factory: Callable[[Path, Path], object] | None = None,
        executor: object | None = None,
        environment_identity: str | None = None,
    ) -> None:
        self.artifact_templates = dict(artifact_templates or {})
        self.validation_implementations = dict(validation_implementations or {})
        self.validation_context_factory = validation_context_factory
        # Plan 4B: a verification command executor decides where/how checks run.
        # Missing environment fails closed; tests may explicitly inject a host executor.
        self.executor = executor
        self.environment_identity = environment_identity

    def verify(
        self,
        *,
        workspace: Path,
        output_dir: Path | None = None,
        transition: TransitionDefinition,
        submission: CompletionSubmission,
        execution_contract: ExecutionContract | None = None,
        candidate_id: str | None = None,
        candidate_manifest_sha256: str | None = None,
        executor: object | None = None,
        environment_identity: str | None = None,
        review_transition: bool = False,
    ) -> VerificationResult:
        errors: list[DomainError] = []
        if review_transition:
            errors.extend(_validate_review_result(submission.review_result))
        report: VerificationReport | None = None
        if (
            execution_contract is not None
            and execution_contract.transition != transition
        ):
            errors.append(_error(
                "execution_contract.transition_mismatch",
                "Verifier transition does not match the frozen execution contract.",
                transition.id,
            ))
        if execution_contract is not None:
            effective_environment_identity = environment_identity if environment_identity is not None else self.environment_identity
            effective_executor = executor if executor is not None else self.executor
            request = verification_request_from_execution_contract(
                contract=execution_contract,
                candidate_id=candidate_id,
                candidate_manifest_sha256=candidate_manifest_sha256,
                environment_identity=effective_environment_identity,
                project_image_identity=getattr(getattr(effective_executor, "environment", None), "project_image_identity", None),
            )
            report, enforcement_errors = self._enforce_execution_contract(
                workspace=workspace,
                contract=execution_contract,
                request=request,
                executor=effective_executor,
                environment_identity=effective_environment_identity,
            )
            errors.extend(enforcement_errors)
        if report is not None:
            if not report.accepted_inline:
                already_signaled = any(
                    error.code.startswith("verification.")
                    and error.code != "verification.incomplete"
                    for error in errors
                )
                if not already_signaled:
                    errors.append(_error(
                        "verification.incomplete",
                        _verification_incomplete_message(report),
                        report.candidate_id,
                    ))
            if report.source_mutated:
                errors.append(_error(
                    "verification.source_mutation",
                    "Verification mutated tracked source; the candidate is rejected.",
                    report.candidate_id,
                ))
        output_root = output_dir or workspace / "output"
        submitted_artifacts = normalize_artifacts(submission.artifacts)
        duplicate_artifact_types = _duplicates(artifact.type for artifact in submitted_artifacts)
        duplicate_artifact_paths = _duplicates(artifact.path for artifact in submitted_artifacts)
        duplicate_changed_files = _duplicates(submission.changed_files)
        multi_artifact_type = transition.derives.artifact_type if transition.derives is not None else None
        for artifact_type in duplicate_artifact_types:
            if artifact_type == multi_artifact_type:
                continue
            errors.append(_error(
                "completion.artifact_duplicate_type",
                f"Artifact type was submitted more than once: {artifact_type}",
                artifact_type,
            ))
        for artifact_path in duplicate_artifact_paths:
            errors.append(_error(
                "completion.artifact_duplicate_path",
                f"Artifact path was submitted more than once: {artifact_path}",
                artifact_path,
            ))
        for changed_file in duplicate_changed_files:
            errors.append(_error(
                "completion.changed_file_duplicate",
                f"Changed file was submitted more than once: {changed_file}",
                changed_file,
            ))
        artifact_types = {artifact.type for artifact in submitted_artifacts}

        for artifact in transition.requires.artifacts:
            if artifact not in artifact_types:
                errors.append(_error(
                    "completion.artifact_missing",
                    f"Required artifact was not submitted: {artifact}",
                    artifact,
                ))

        allowed_artifact_types = set(transition.requires.artifacts)
        if multi_artifact_type is not None:
            allowed_artifact_types.add(multi_artifact_type)
            if transition.derives.required and multi_artifact_type not in artifact_types:
                errors.append(_error(
                    "completion.derived_task_missing",
                    f"Deriving transition requires at least one {multi_artifact_type} artifact.",
                    multi_artifact_type,
                ))

        for artifact in submitted_artifacts:
            if artifact.type not in allowed_artifact_types:
                errors.append(_error(
                    "completion.artifact_unexpected",
                    f"Artifact type is not required by this transition: {artifact.type}",
                    artifact.type,
                ))
            template = self.artifact_templates.get(artifact.type)
            if template and "{" not in template and artifact.path != template:
                errors.append(_error(
                    "completion.artifact_template_mismatch",
                    f"Artifact path must match template for {artifact.type}: {template}",
                    artifact.path,
                ))
            artifact_path = _contained_path(output_root, artifact.path)
            if artifact_path is None:
                errors.append(_error(
                    "completion.artifact_outside_output",
                    f"Artifact path escapes the shared output directory: {artifact.path}",
                    artifact.path,
                ))
                continue
            if _escapes_via_symlink(output_root, artifact.path):
                errors.append(_error(
                    "completion.artifact_symlink_escape",
                    f"Artifact path escapes the shared output directory through a symlink: {artifact.path}",
                    artifact.path,
                ))
                continue
            if not artifact_path.is_file():
                errors.append(_error(
                    "completion.artifact_not_found",
                    f"Submitted artifact does not exist: {artifact.path}",
                    artifact.path,
                ))
                continue
            if artifact_path.stat().st_size == 0:
                errors.append(_error(
                    "completion.artifact_empty",
                    f"Submitted artifact is empty: {artifact.path}",
                    artifact.path,
                ))
            if artifact.sha256 is not None:
                actual_hash = _sha256(artifact_path)
                if actual_hash != artifact.sha256.lower():
                    errors.append(_error(
                        "completion.artifact_hash_mismatch",
                        f"Submitted artifact hash does not match: {artifact.path}",
                        artifact.path,
                    ))

        required_validations = tuple(call.type for call in transition.requires.validations)
        for validation in required_validations:
            evidence = submission.validation_evidence.get(validation)
            if evidence is None or not str(evidence).strip():
                errors.append(_error(
                    "completion.validation_evidence_missing",
                    f"Validation evidence is missing for {validation}.",
                    validation,
                ))
        errors.extend(self._run_trusted_validations(
            workspace=workspace,
            output_root=output_root,
            transition=transition,
        ))

        if transition.requires.changed_files_required and not submission.changed_files:
            errors.append(_error(
                "completion.changed_files_missing",
                "Changed-file evidence is required for this transition.",
            ))

        for changed_file in submission.changed_files:
            changed_path = _contained_path(workspace, changed_file)
            if changed_path is None:
                errors.append(_error(
                    "completion.changed_file_outside_workspace",
                    f"Changed file path escapes the workspace: {changed_file}",
                    changed_file,
                ))
            elif not changed_path.exists():
                errors.append(_error(
                    "completion.changed_file_not_found",
                    f"Changed file does not exist: {changed_file}",
                    changed_file,
                ))

        actual_changed_files = _git_changed_files(workspace)
        if actual_changed_files is not None:
            submitted = set(submission.changed_files)
            if submitted != actual_changed_files:
                errors.append(_error(
                    "completion.changed_files_mismatch",
                    "Submitted changed files do not match the workspace diff.",
                    ",".join(sorted(actual_changed_files)),
                ))

        if report is not None and errors:
            report = VerificationReport(
                schema=report.schema,
                classification=_failure_classification(errors),
                baseline_sha256=report.baseline_sha256,
                post_manifest_sha256=report.post_manifest_sha256,
                added=report.added,
                edited=report.edited,
                removed=report.removed,
                renamed=report.renamed,
                changed_lines=report.changed_lines,
                checks=report.checks,
                candidate_id=report.candidate_id,
                candidate_manifest_sha256=report.candidate_manifest_sha256,
                request_sha256=report.request_sha256,
                command_policy_sha256=report.command_policy_sha256,
                project_image_identity=report.project_image_identity,
                environment_identity=report.environment_identity,
                not_run_checks=report.not_run_checks,
                source_mutated=report.source_mutated,
                classification_detail=_granular_classification(report, errors),
                coverage_changes=report.coverage_changes,
            )
        return VerificationResult(accepted=not errors, errors=tuple(errors), report=report)

    def _enforce_execution_contract(
        self,
        *,
        workspace: Path,
        contract: ExecutionContract,
        request: VerificationRequest | None = None,
        candidate_id: str | None = None,
        candidate_manifest_sha256: str | None = None,
        executor: object | None = None,
        environment_identity: str | None = None,
    ) -> tuple[VerificationReport, tuple[DomainError, ...]]:
        # The only acceptance criterion is the project's configured global
        # commands. A worker may freely create/edit/rename/delete files required
        # by its task; file diffs are never part of this acceptance decision.
        if request is None:
            request = verification_request_from_execution_contract(
                contract=contract,
                candidate_id=candidate_id,
                candidate_manifest_sha256=candidate_manifest_sha256,
                environment_identity=environment_identity,
            )
        baseline_manifest = contract.baseline_manifest
        baseline_sha = baseline_manifest.sha256
        pre_manifest = _workspace_deliverable_manifest(workspace)
        pre_sha = pre_manifest.sha256
        pre_lockfile = _capture_lockfile_identity(workspace)
        checks, check_errors = _run_contract_checks(workspace, contract, executor)
        # Measure the real source tree before and after the checks ran. Source
        # mutation is a post-tree differing from the pre-verification tree;
        # cache/build outputs excluded by snapshot rules are not tracked source.
        # Plan 4A: never repeat the baseline digest as the candidate/post digest
        # unless the actual source tree is unchanged by verification.
        post_manifest = _workspace_deliverable_manifest(workspace)
        post_sha = post_manifest.sha256
        post_lockfile = _capture_lockfile_identity(workspace)
        # Coverage regression guard (plan 4E): record baseline-to-candidate
        # changes on test-discovery/build-script paths so review can assess
        # whether a suite was disabled to make a global command exit zero.
        coverage_changes = _coverage_changes(baseline_manifest, post_manifest)
        not_run = sum(1 for check in checks if check.status == VERIFICATION_NOT_RUN_STATUS)
        source_mutated = post_sha != pre_sha
        all_errors = list(check_errors)
        lockfile_mutated = pre_lockfile is not None and post_lockfile is not None and pre_lockfile.sha256 != post_lockfile.sha256
        if lockfile_mutated:
            source_mutated = True
            all_errors.append(_error(
                "verification.lockfile_mutated",
                "Verification rewrote committed dependency locks; the candidate is rejected.",
                request.candidate_id,
            ))
        return VerificationReport(
            VERIFICATION_REPORT_SCHEMA_V2,
            None,
            baseline_sha,
            post_sha,
            (),
            (),
            (),
            (),
            0,
            checks,
            request.candidate_id,
            request.candidate_manifest_sha256,
            request.sha256,
            request.command_policy_sha256,
            request.project_image_identity,
            request.environment_identity,
            not_run,
            source_mutated,
            _granular_classification(None, all_errors, not_run=not_run, source_mutated=source_mutated),
            coverage_changes,
        ), tuple(all_errors)

    def _run_trusted_validations(
        self,
        *,
        workspace: Path,
        output_root: Path,
        transition: TransitionDefinition,
    ) -> tuple[DomainError, ...]:
        errors: list[DomainError] = []
        if not transition.requires.validations:
            return ()
        if self.validation_context_factory is None:
            return tuple(_error(
                "completion.validation_unavailable",
                "Trusted validation runtime is not configured.",
                call.type,
            ) for call in transition.requires.validations)
        with tempfile.TemporaryDirectory(prefix="open-tulid-validation-") as temp_dir:
            validation_workspace = Path(temp_dir) / "workspace"
            shutil.copytree(workspace, validation_workspace, symlinks=True)
            _make_tree_user_writable(validation_workspace)
            validation_output = _map_validation_output_root(
                workspace=workspace,
                output_root=output_root,
                validation_workspace=validation_workspace,
            )
            context = self.validation_context_factory(validation_workspace, validation_output)
            for call in transition.requires.validations:
                implementation = self.validation_implementations.get(call.type)
                if implementation is None:
                    errors.append(_error(
                        "completion.validation_unimplemented",
                        f"No trusted validation implementation is installed for {call.type}.",
                        call.type,
                    ))
                    continue
                try:
                    result = implementation(context, **dict(call.args))
                except Exception as exc:
                    errors.append(_error(
                        "completion.validation_error",
                        f"Trusted validation {call.type} raised: {exc}",
                        call.type,
                    ))
                    continue
                if not bool(getattr(result, "passed", False)):
                    message = str(getattr(result, "message", "") or "validation failed")
                    errors.append(_error(
                        "completion.validation_failed",
                        f"Trusted validation {call.type} failed: {message}",
                        call.type,
                    ))
        return tuple(errors)


# Coverage-relevant paths (plan 4E): test files, test-discovery configuration,
# and build/discovery scripts whose composition can change without a later global
# command failing. Mirrors the project-owned discovery surface so a disabled
# suite is surfaced for review rather than accepted merely on a zero exit.
COVERAGE_RELEVANT_PATTERNS = (
    "test_*",
    "*_test.py",
    "*_test.pyc",
    "*.test.js",
    "*.test.jsx",
    "*.test.ts",
    "*.test.tsx",
    "*.spec.js",
    "*.spec.jsx",
    "*.spec.ts",
    "*.spec.tsx",
    "tests/**",
    "test/**",
    "spec/**",
    "__tests__/**",
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "noxfile.py",
    "setup.cfg",
    "jest.config*",
    "vitest.config*",
    "karma.conf*",
    "playwright.config*",
    "cypress.config*",
    "pyproject.toml",
    "package.json",
    "Makefile",
    "makefile",
    "*.mk",
    "justfile",
    "Dockerfile",
    "Dockerfile.*",
    "docker-compose*.yml",
    "docker-compose*.yaml",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)


def _coverage_relevant_path(path: str) -> bool:
    for pattern in COVERAGE_RELEVANT_PATTERNS:
        if fnmatch.fnmatchcase(path, pattern):
            return True
    return False


def _coverage_changes(
    baseline: BaselineManifest | None,
    post: BaselineManifest,
) -> tuple[CoverageChange, ...]:
    """Baseline-to-candidate changes restricted to coverage-relevant paths."""
    if baseline is None:
        return ()
    before = {entry.path: entry for entry in baseline.entries}
    after = {entry.path: entry for entry in post.entries}
    changes: list[CoverageChange] = []
    for path in sorted(set(after) - set(before)):
        entry = after[path]
        if _coverage_relevant_path(path):
            changes.append(CoverageChange("add", path, None, entry.sha256))
    for path in sorted(set(before) - set(after)):
        entry = before[path]
        if _coverage_relevant_path(path):
            changes.append(CoverageChange("delete", path, entry.sha256, None))
    for path in sorted(set(before) & set(after)):
        if before[path].sha256 != after[path].sha256 and _coverage_relevant_path(path):
            changes.append(CoverageChange(
                "edit", path, before[path].sha256, after[path].sha256,
            ))
    return tuple(changes)


def _manifest_changes(
    baseline: BaselineManifest,
    post: BaselineManifest,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]:
    before = {entry.path: entry for entry in baseline.entries}
    after = {entry.path: entry for entry in post.entries}
    added = set(after) - set(before)
    removed = set(before) - set(after)
    edited = tuple(sorted(path for path in set(before) & set(after) if before[path].sha256 != after[path].sha256
                          or (before[path].mode is not None and before[path].mode != after[path].mode)))
    renamed: list[tuple[str, str]] = []
    for old in sorted(removed):
        matches = sorted(new for new in added if before[old].sha256 == after[new].sha256)
        if len(matches) == 1:
            new = matches[0]
            renamed.append((old, new))
            added.remove(new)
            removed.remove(old)
    return tuple(sorted(added)), edited, tuple(sorted(removed)), tuple(renamed)


def _path_allowed(path: str, patterns: Sequence[str]) -> bool:
    from fnmatch import fnmatchcase
    for pattern in patterns:
        if fnmatchcase(path, pattern):
            return True
        if _path_under_directory(path, pattern):
            return True
    return False


def _path_under_directory(path: str, base: str) -> bool:
    base_parts = Path(base).parts
    path_parts = Path(path).parts
    if not base_parts or len(path_parts) < len(base_parts):
        return False
    return path_parts[: len(base_parts)] == base_parts and base_parts[-1] not in {"", "."}


def _changed_line_count(workspace: Path, baseline: BaselineManifest, paths: Sequence[str]) -> int:
    total = 0
    for path in paths:
        new_path = workspace / path
        new = _read_text(new_path) if new_path.is_file() else ""
        if new is None:
            total += 1
            continue
        # The frozen manifest deliberately contains only hashes and sizes, never
        # source content. Count the complete current file as a conservative upper
        # bound for each added or edited file; removals count as one unit.
        total += max(1, len(new.splitlines()))
    return total


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _run_contract_checks(
    workspace: Path,
    contract: ExecutionContract,
    executor: object | None = None,
) -> tuple[tuple[VerificationCheckResult, ...], tuple[DomainError, ...]]:
    results: list[VerificationCheckResult] = []
    errors: list[DomainError] = []
    command_executor = executor or _default_executor()
    for check in contract.resolved_checks:
        if check.runner != "command":
            continue
        outcome = command_executor.execute(_as_verification_command(check), workspace)
        log_root = workspace / ".open-tulid" / "verification-logs"
        log_root.mkdir(parents=True, exist_ok=True)
        refs = []
        for stream in ("stdout", "stderr"):
            path = log_root / f"{len(results):03d}.{stream}.log"
            path.write_text(getattr(outcome.check, stream), encoding="utf-8")
            refs.append(str(path))
        results.append(replace(
            outcome.check,
            stdout=_bounded_excerpt(outcome.check.stdout),
            stderr=_bounded_excerpt(outcome.check.stderr),
            log_refs=tuple(refs),
        ))
        if outcome.error is not None:
            errors.append(outcome.error)
    return tuple(results), tuple(errors)


def _default_executor():
    # Missing environment is an explicit blocker. Host execution is available
    # only through explicit injection by deterministic tests.
    from open_tulid.runtime.verification_runtime import ContainerCommandExecutor, VerificationEnvironment
    return ContainerCommandExecutor(environment=VerificationEnvironment())


def _as_verification_command(check) -> VerificationCommand:
    return VerificationCommand(
        name=check.id,
        argv=tuple(check.argv),
        working_directory=check.working_directory,
        timeout_seconds=check.timeout_seconds,
        expected_exit_code=check.expect.exit_code,
    )


def _capture_lockfile_identity(workspace: Path):
    from open_tulid.runtime.verification_runtime import capture_lockfile_identity
    if not workspace.is_dir():
        return None
    try:
        return capture_lockfile_identity(workspace)
    except OSError:
        return None


def _workspace_deliverable_manifest(workspace: Path) -> BaselineManifest:
    """Real deliverable manifest of the workspace (used for digest recomputation)."""
    from .candidate import capture_deliverable_manifest
    return capture_deliverable_manifest(workspace)


def _workspace_manifest_sha256(workspace: Path) -> str:
    """Real post-verification source digest of the workspace (or baseline-less "").

    Used to stop a report from blindly repeating the baseline digest as the
    candidate/post digest when the source tree actually changed.
    """
    return _workspace_deliverable_manifest(workspace).sha256


def _verification_incomplete_message(report: "VerificationReport") -> str:
    if not report.checks:
        return "Verification produced an empty report; an empty report is never success."
    if report.not_run_checks:
        return f"Verification left {report.not_run_checks} command(s) never run; never-run commands are not success."
    return "Not every global command passed."


def _failure_classification(errors: Sequence[DomainError]) -> str:
    codes = {error.code for error in errors}
    if any(code.startswith("verification.baseline") for code in codes):
        return "baseline_failure"
    if any(
        code in {"verification.check_environment", "verification.check_timeout"}
        or code.startswith("verification.env_")
        or code == "verification.lockfile_mutated"
        for code in codes
    ):
        return "environment_failure"
    if any(code.startswith("execution_contract") or code.startswith("verification.path") or code.startswith("verification.deletion") or code.startswith("verification.rename") or code.startswith("verification.max_files") or code.startswith("verification.changed_line_budget") for code in codes):
        return "contract_failure"
    return "implementation_failure"


def _granular_classification(
    report: "VerificationReport | None" = None,
    errors: Sequence[DomainError] = (),
    *,
    not_run: int | None = None,
    source_mutated: bool | None = None,
) -> str | None:
    """Granular outcome recorded separately from pass/fail (plan 4A).

    Order of checks matters: source mutation and infrastructure interruption
    dominate, then policy/environment/timeout, then failed behavior.
    """
    if report is not None and report.source_mutated:
        return VERIFICATION_CLASSIFICATION_SOURCE_MUTATION
    if source_mutated:
        return VERIFICATION_CLASSIFICATION_SOURCE_MUTATION
    not_run_count = report.not_run_checks if report is not None else not_run
    if report is not None and not report.checks:
        return VERIFICATION_CLASSIFICATION_INFRASTRUCTURE_INTERRUPTION
    if not_run_count:
        return VERIFICATION_CLASSIFICATION_INFRASTRUCTURE_INTERRUPTION
    codes = {error.code for error in errors}
    if "verification.source_mutation" in codes:
        return VERIFICATION_CLASSIFICATION_SOURCE_MUTATION
    if any(code.startswith("contract.") for code in codes):
        return VERIFICATION_CLASSIFICATION_MALFORMED_POLICY
    dependency_codes = (
        "verification.dependency",
        "verification.env_",
        "verification.lockfile_mutated",
        "contract.check_environment",
    )
    if any(code.startswith(code_prefix) for code in codes for code_prefix in dependency_codes):
        return VERIFICATION_CLASSIFICATION_DEPENDENCY_PREPARATION
    if "verification.check_environment" in codes:
        return VERIFICATION_CLASSIFICATION_ENVIRONMENT_UNAVAILABLE
    if "verification.check_timeout" in codes:
        return VERIFICATION_CLASSIFICATION_COMMAND_TIMEOUT
    if "verification.incomplete" in codes:
        return VERIFICATION_CLASSIFICATION_INFRASTRUCTURE_INTERRUPTION
    if any(codes):
        return VERIFICATION_CLASSIFICATION_FAILED_BEHAVIOR
    return None


def submission_from_mapping(payload: Mapping[str, object]) -> CompletionSubmission:
    evidence = payload.get("validation_evidence", {})
    if not isinstance(evidence, Mapping):
        evidence = {}
    review_result = payload.get("review_result")
    parsed_review = None
    if review_result is not None:
        if not isinstance(review_result, Mapping):
            parsed_review = {"_invalid": "review_result must be an object"}
        else:
            validation_errors = _validate_review_result(review_result)
            parsed_review = dict(review_result)
            if validation_errors:
                parsed_review["_review_result_invalid"] = ", ".join(
                    item.message for item in validation_errors if isinstance(item, DomainError)
                )
    return CompletionSubmission(
        submission_id=_optional_string(payload.get("submission_id")),
        attempt=_optional_int(payload.get("attempt")),
        summary=str(payload.get("summary", "")),
        artifacts=_artifact_tuple(payload.get("artifacts", ())),
        changed_files=_string_tuple(payload.get("changed_files", ())),
        validation_evidence={str(key): str(value) for key, value in evidence.items()},
        review_result=parsed_review,
    )


def _validate_review_result(
    review_result: Mapping[str, object] | None,
) -> tuple[DomainError, ...]:
    """Structural validation of the compact requirement-to-evidence review result.

    A review must name the behavior and cite the source/test evidence inspected,
    and must be an object. A remaining blocker is recorded as a distinct blocker
    for the clarification/planning path: it prevents this completion from being
    treated as verified implementation success.
    """
    if review_result is None:
        return (_error(
            "completion.review_result_missing",
            "A review transition requires a compact review_result naming the behavior, "
            "the source/test evidence, any defects/fixes, and remaining blockers.",
            "review_result",
        ),)
    if "_invalid" in review_result or "_review_result_invalid" in review_result:
        return (_error(
            "completion.review_result_invalid",
            "Review result is malformed; it must be an object with behavior/evidence fields.",
            "review_result",
        ),)
    behavior = review_result.get("behavior")
    evidence = review_result.get("evidence")
    errors: list[DomainError] = []
    if not isinstance(behavior, str) or not behavior.strip():
        errors.append(_error(
            "completion.review_result_behavior_missing",
            "Review result must name at least one required behavior.",
            "review_result.behavior",
        ))
    if not isinstance(evidence, str) or not evidence.strip():
        errors.append(_error(
            "completion.review_result_evidence_missing",
            "Review result must cite the source/test evidence inspected for the reported behavior.",
            "review_result.evidence",
        ))
    blockers = review_result.get("remaining_blockers", ())
    if isinstance(blockers, Sequence) and not isinstance(blockers, (str, bytes)):
        for raw in blockers:
            if not isinstance(raw, str) or not raw.strip():
                errors.append(_error(
                    "completion.review_result_blocker_invalid",
                    "A remaining blocker must be a non-empty string.",
                    "review_result.remaining_blockers",
                ))
        if any(isinstance(raw, str) and raw.strip() for raw in blockers):
            errors.append(_error(
                "completion.review_blocked",
                "Review found remaining product blockers; route them to the clarification/planning "
                "path instead of recording verified implementation success.",
                "review_result.remaining_blockers",
            ))
    return tuple(errors)


def _contained_path(root_path: Path, value: str) -> Path | None:
    if not value or Path(value).is_absolute():
        return None
    candidate = (root_path / value).resolve()
    root = root_path.resolve()
    if candidate == root or root in candidate.parents:
        return candidate
    return None


def _escapes_via_symlink(root_path: Path, value: str) -> bool:
    root = root_path.resolve()
    current = root
    for part in Path(value).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            return True
        current = current / part
        if current.is_symlink():
            resolved = current.resolve()
            if resolved != root and root not in resolved.parents:
                return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_tuple(value: object) -> tuple[ArtifactSubmission, ...]:
    if isinstance(value, str):
        return (ArtifactSubmission(type=value, path=value),)
    if not isinstance(value, Sequence):
        return ()
    artifacts: list[ArtifactSubmission] = []
    for item in value:
        if isinstance(item, str):
            clean = item.strip()
            if clean:
                artifacts.append(ArtifactSubmission(type=clean, path=clean))
            continue
        if not isinstance(item, Mapping):
            continue
        artifact_type = _optional_string(item.get("type"))
        path = _optional_string(item.get("path"))
        if artifact_type is None or path is None:
            continue
        artifacts.append(ArtifactSubmission(
            type=artifact_type,
            path=path,
            sha256=_optional_string(item.get("sha256")),
        ))
    return tuple(artifacts)


def normalize_artifacts(value: Sequence[ArtifactSubmission | str]) -> tuple[ArtifactSubmission, ...]:
    artifacts: list[ArtifactSubmission] = []
    for item in value:
        if isinstance(item, ArtifactSubmission):
            artifacts.append(item)
        else:
            clean = str(item).strip()
            if clean:
                artifacts.append(ArtifactSubmission(type=clean, path=clean))
    return tuple(artifacts)


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if not isinstance(value, Sequence):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _error(code: str, message: str, location: str | None = None) -> DomainError:
    return DomainError(code=code, message=message, location=location)


def _duplicates(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for raw in values:
        value = str(raw)
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return tuple(duplicates)


def _map_validation_output_root(*, workspace: Path, output_root: Path, validation_workspace: Path) -> Path:
    try:
        relative_output = output_root.resolve().relative_to(workspace.resolve())
    except ValueError:
        return validation_workspace / "output"
    return validation_workspace / relative_output


def _make_tree_user_writable(root: Path) -> None:
    for path in tuple(root.rglob("*")) + (root,):
        try:
            mode = path.lstat().st_mode
        except OSError:
            continue
        if stat.S_ISLNK(mode):
            continue
        writable_mode = mode | stat.S_IRUSR | stat.S_IWUSR
        if stat.S_ISDIR(mode):
            writable_mode |= stat.S_IXUSR
        try:
            path.chmod(writable_mode)
        except OSError:
            continue


def _git_changed_files(workspace: Path) -> set[str] | None:
    if not (workspace / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ("git", "status", "--porcelain"),
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    changed: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            changed.add(path)
    return changed


def _bounded_excerpt(value: str) -> str:
    if len(value) <= LOG_EXCERPT_CHARACTER_LIMIT:
        return value
    marker = "\n[output omitted; full log retained as artifact]"
    return value[:LOG_EXCERPT_CHARACTER_LIMIT - len(marker)] + marker
