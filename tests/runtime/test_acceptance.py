from dataclasses import replace

import pytest

from open_tulid.domain import ExecutionJob, Task, TransitionDefinition, RequirementDefinition
from open_tulid.runtime.acceptance import accepted_task_evidence
from open_tulid.runtime.attempts import task_semantic_revision
from open_tulid.runtime.events import TransactionJournalStore
from types import SimpleNamespace
import subprocess


@pytest.mark.parametrize("state", ["accepted", "descendant", "reset", "missing_identity"])
def test_code_acceptance_requires_delivered_commit_in_current_history(tmp_path, state):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    (repo / "app.txt").write_text("baseline")
    git("add", ".")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")
    (repo / "app.txt").write_text("accepted implementation")
    git("commit", "-qam", "implementation")
    accepted_commit = git("rev-parse", "HEAD")
    if state == "descendant":
        (repo / "next.txt").write_text("subsequent task")
        git("add", ".")
        git("commit", "-qm", "next task")
    elif state == "reset":
        git("reset", "--hard", baseline)
    task = Task(id="task", title="API", path="task.md", task_type="ImplementationTask", current_state="Done")
    transition = TransitionDefinition(id="Implement", task_type=task.task_type, from_state="Todo", to_state="Done", worker="chosen", requires=RequirementDefinition(), transaction=None, review=False)
    workflow = SimpleNamespace(transitions={transition.id: transition}, task_types={task.task_type: object()})
    journals = TransactionJournalStore(tmp_path / "journals")
    record = journals.prepare(journal_id="transaction", project_id="project", task_id=task.id,
        transition_id=transition.id, effects=(), events=(), context={
            "job_id": "job", "task_revision": task_semantic_revision(task),
            "expected_to_state": "Done", "verification_accepted": True,
            "repository_base_commit": baseline,
            "candidate_id": "candidate", "candidate_manifest_sha256": "digest",
            "verification_report": {"checks": [{"status": "passed"}], "candidate_id": "candidate", "candidate_manifest_sha256": "digest"},
        }).record
    journals.commit(record)
    job = ExecutionJob(job_id="job", project_id="project", task_id=task.id, transition_id=transition.id,
        worker_id="chosen", workspace_path=str(tmp_path), status="accepted", metadata={
            "acceptance_transaction_id": "transaction",
            "acceptance_repository_commit": None if state == "missing_identity" else accepted_commit,
        })
    assert accepted_task_evidence(job, task=task, workflow=workflow, journals=journals, repo_root=repo) == (state in {"accepted", "descendant"})


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
