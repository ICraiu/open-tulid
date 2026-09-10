"""Acceptance evidence shared by dependency admission and manual completion."""
from __future__ import annotations

from typing import Mapping
import re
import subprocess

from open_tulid.domain.completion import SuccessfulCompletionCriteria, is_review_transition
from .attempts import task_semantic_revision
from .task_contracts import task_uses_global_contract
from .verifier import _validate_review_result


def accepted_task_evidence(job, *, task, workflow, journals, source_identities=(), target_state=None, repo_root=None) -> bool:
    if str(getattr(job.status, "value", job.status)) != "accepted":
        return False
    transition = workflow.transitions.get(job.transition_id)
    if transition is None or transition.task_type != task.task_type:
        return False
    transaction_id = job.metadata.get("acceptance_transaction_id")
    if not isinstance(transaction_id, str) or journals is None:
        return False
    try:
        journal = journals.load(transaction_id)
    except (OSError, ValueError, TypeError):
        return False
    if journal.task_id != task.id or journal.project_id != job.project_id or journal.transition_id != job.transition_id:
        return False
    context = journal.context
    revision = task_semantic_revision(task, source_identities=source_identities)
    if context.get("task_revision") != revision or context.get("job_id") != job.job_id:
        return False
    code_task = task_uses_global_contract(task, workflow)
    if code_task and context.get("repository_base_commit"):
        commit = job.metadata.get("acceptance_repository_commit")
        if repo_root is None or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{7,64}", commit):
            return False
        try:
            ancestry = subprocess.run(
                ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
                cwd=repo_root, capture_output=True, timeout=10,
            )
            if ancestry.returncode != 0:
                return False
        except (OSError, subprocess.TimeoutExpired):
            return False
    report = context.get("verification_report")
    verified = context.get("verification_accepted") is True
    if code_task:
        checks = report.get("checks", ()) if isinstance(report, Mapping) else ()
        verified = bool(verified and checks and all(
            isinstance(check, Mapping) and check.get("status") == "passed" for check in checks
        ) and not report.get("source_mutated") and not report.get("not_run_checks")
            and report.get("candidate_id") == context.get("candidate_id")
            and report.get("candidate_manifest_sha256") == context.get("candidate_manifest_sha256"))
    review = not is_review_transition(transition) or not _validate_review_result(job.metadata.get("review_result"))
    criteria = SuccessfulCompletionCriteria(
        acceptance_verified=verified,
        delivery_transaction_committed=str(getattr(journal.status, "value", journal.status)) == "committed",
        review_satisfied=review,
        final_state_consistent=context.get("expected_to_state") == (target_state or task.current_state),
    )
    return criteria.succeeded()
