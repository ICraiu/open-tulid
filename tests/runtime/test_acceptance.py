from dataclasses import replace

import pytest

from open_tulid.domain import ExecutionJob, Task, TransitionDefinition, RequirementDefinition
from open_tulid.runtime.acceptance import accepted_task_evidence
from open_tulid.runtime.attempts import task_semantic_revision
from open_tulid.runtime.events import TransactionJournalStore
from types import SimpleNamespace


@pytest.mark.parametrize("defect", [None, "old_revision", "skipped_review", "pending_journal",
                                    "missing_review", "failed_checks", "wrong_candidate"])
def test_acceptance_requires_current_revision_final_transition_and_committed_proof(tmp_path, defect):
    task = Task(id="task", title="API", path="task.md", task_type="Widget", current_state="Shipped", body="## What\nRequired behavior")
    transition = TransitionDefinition(id="Audit", task_type="Widget", from_state="Inspect",
        to_state="Shipped", worker="chosen", requires=RequirementDefinition(), transaction=None, review=True)
    workflow = SimpleNamespace(transitions={"Audit": transition}, task_types={"Widget": object()})
    journals = TransactionJournalStore(tmp_path / "journals")
    context = {"job_id": "job", "task_revision": task_semantic_revision(task),
        "expected_to_state": "Shipped", "verification_accepted": True,
        "candidate_id": "candidate", "candidate_manifest_sha256": "digest",
        "verification_report": {"checks": [{"status": "passed"}],
            "candidate_id": "candidate", "candidate_manifest_sha256": "digest"}}
    if defect == "old_revision":
        context["task_revision"] = task_semantic_revision(replace(task, body="## What\nOld requirements"))
    if defect == "skipped_review":
        context["expected_to_state"] = "Inspect"
    if defect == "failed_checks":
        context["verification_report"]["checks"][0]["status"] = "failed"
    if defect == "wrong_candidate":
        context["verification_report"]["candidate_id"] = "other"
    record = journals.prepare(journal_id="transaction", project_id="project", task_id="task",
        transition_id="Audit", effects=(), events=(), context=context).record
    if defect != "pending_journal":
        journals.commit(record)
    job = ExecutionJob(job_id="job", project_id="project", task_id="task", transition_id="Audit",
        worker_id="chosen", workspace_path=str(tmp_path), status="accepted", metadata={
            "acceptance_transaction_id": "transaction",
            "review_result": None if defect == "missing_review" else {"behavior": "API", "evidence": "api.test"}})
    assert accepted_task_evidence(job, task=task, workflow=workflow, journals=journals) == (defect is None)
