#!/usr/bin/env python3
"""Deterministic scripted worker for Tulid reliability fault-chain E2E tests.

The worker is not a model: it is the deterministic actuator that Tulid's
scripted runtime exercises. It responds to the transition and to the scenario /
fault selected by the fixture, so the same container can stand in for several
configured ``<workers>`` and can deterministically inject the known failures
that plan 6E must prove bounded.
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request


workspace = pathlib.Path.cwd()
context_path = workspace / ".open-tulid" / "job-context.json"
prompt_path = workspace / ".open-tulid" / "prompt-packet.md"
context = json.loads(context_path.read_text(encoding="utf-8"))
prompt = prompt_path.read_text(encoding="utf-8")
endpoint = os.environ["OPEN_TULID_COMPLETION_ENDPOINT"]
token = os.environ["OPEN_TULID_COMPLETION_TOKEN"]
output = pathlib.Path(os.environ["OPEN_TULID_OUTPUT_DIR"])

transition_id = context["transition_id"]
task_id = context["task_id"]
worker_id = context.get("worker_id", "unknown")
scenario = os.environ.get("SCRIPTED_RUNTIME_SCENARIO", "default")
session_token = os.environ.get("OPEN_TULID_MODEL_SESSION_TOKEN", "")
model_endpoint = os.environ.get("OPEN_TULID_MODEL_ENDPOINT", "")

print(f"scripted runtime worker scenario={scenario} transition={transition_id} task={task_id} worker={worker_id}")
print(f"prompt-bytes={len(prompt.encode('utf-8'))}")
print(f"uid={os.getuid()} gid={os.getgid()}")


def submit(payload):
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-open-tulid-completion-token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            print(f"completion status={response.status} body={body}")
            return response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        print(f"completion status={exc.code} body={body}")
        return exc.code


def write_output(name, content):
    target = output / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def append_workspace_file(name, text):
    path = workspace / name
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + text, encoding="utf-8")


def is_fault(name):
    return name in scenario


def role_of(transition):
    """A canonical role derived from the transition, never from worker name.

    State/task-type names are free-form in plan 6E; the worker decides what to
    do from the transition's semantic position, not from the state spelling.
    """
    frozen_path = workspace / ".open-tulid/execution-contract.json"
    if frozen_path.exists():
        frozen = json.loads(frozen_path.read_text())
        if frozen.get("transition", {}).get("review") is True:
            return "review"
    lowered = transition.lower()
    if "draftdirection" in lowered or "direction" in lowered:
        return "direction"
    if "spec" in lowered and ("write" in lowered or "draft" in lowered):
        return "spec"
    if "breakdown" in lowered or "derive" in lowered:
        return "breakdown"
    if "review" in lowered or "revise" in lowered:
        return "review"
    if "implement" in lowered or "code" in lowered or "build" in lowered:
        return "implement"
    return "direction"


def review_result():
    return {
        "behavior": "health endpoint returns expected response",
        "evidence": "app.py handler and check_repo.py verify the behavior",
        "defects_fixes": [],
        "remaining_blockers": [],
    }


def validation_evidence():
    return {
        "tests_pass": "passed",
        "project_build": "passed",
    }


def derived_task(local_id, title, body, dependencies=()):
    frontmatter = [f"local_id: {local_id}"]
    if dependencies:
        frontmatter.append(f"dependencies: [{', '.join(dependencies)}]")
    return (
        "---\n"
        + "\n".join(frontmatter)
        + "\n---\n"
        + body
    )


def health_task_body():
    return "{title}\n\nAdd a concrete health check entrypoint and preserve the clipboard-ready flow.\n\n## Why\nTurn speech into structured text without leaving the clipboard workflow.\n\n## What\nDefine the speech-to-text flow and its concrete implementation tasks.\n\n## How\nTouch the task breakdown and direction seams.\n\n## Acceptance\n- the speech-to-text flow is described\n- the breakdown produces concrete implementation tasks\n".format(
        title="# Implement healthz endpoint"
    )


def helper_task_body():
    return (
        "# Implement shared helper\n\n"
        "Add a small shared helper module.\n"
        "## Why\nReuse speech-to-text plumbing across screens.\n"
        "## What\nProvide a reusable helper across implementation tasks.\n"
        "## How\nEdit the helper seam and keep the direction stable.\n"
        "## Acceptance\n- a shared helper is available to downstream tasks\n"
    )


def planning_artifacts():
    return {
        "product-spec.md": "# Product Spec\n\n## Problem\nCapture clipboard audio and speech reliably.\n\n## Requirements\nProvide a tray-triggered transcription flow.\n",
        "technical-direction.md": "# Technical Direction\n\n## Architecture\nUse a small local service and a clipboard bridge.\n\n## Interfaces\nExpose a single transcription entrypoint.\n",
    }


def spec_artifact():
    return "# Implementation Spec\n\n## Modules\nAdd clipboard capture, speech orchestration, and result insertion.\n\n## Testing\nUse deterministic repo checks for the E2E workflow.\n"


def direction_payload(attempt=1):
    files = planning_artifacts()
    write_output("product-spec.md", files["product-spec.md"])
    write_output("technical-direction.md", files["technical-direction.md"])
    return {
        "submission_id": "draft-direction",
        "attempt": attempt,
        "summary": "direction drafted by scripted worker",
        "artifacts": [
            {"type": "ProductSpec", "path": "product-spec.md"},
            {"type": "TechnicalDirection", "path": "technical-direction.md"},
        ],
        "changed_files": [],
        "validation_evidence": {},
    }


def spec_payload(attempt=1):
    write_output("implementation-spec.md", spec_artifact())
    return {
        "submission_id": "implementation-spec",
        "attempt": attempt,
        "summary": "implementation spec drafted by scripted worker",
        "artifacts": [
            {"type": "ImplementationSpec", "path": "implementation-spec.md"},
        ],
        "changed_files": [],
        "validation_evidence": {},
    }


def breakdown_payload(attempt=1):
    batch = is_fault("batch")
    if is_fault("malformed_artifact"):
        write_output(
            "tasks/a-task.md",
            "# Broken derived task\n\nMissing the required frontmatter shape.\n",
        )
        return {
            "submission_id": "breakdown-malformed",
            "attempt": attempt,
            "summary": "deliberately malformed planning artifacts",
            "artifacts": [{"type": "ImplementationTaskFile", "path": "tasks/a-task.md"}],
            "changed_files": [],
            "validation_evidence": {},
        }
    if batch:
        write_output("tasks/a-task.md", derived_task("a", "# Implement healthz endpoint", health_task_body()))
        write_output(
            "tasks/b-task.md",
            derived_task("b", "# Implement shared helper", helper_task_body(), dependencies=["a"]),
        )
        return {
            "submission_id": "breakdown-batch",
            "attempt": attempt,
            "summary": "implementation batch derived by scripted worker",
            "artifacts": [
                {"type": "ImplementationTaskFile", "path": "tasks/a-task.md"},
                {"type": "ImplementationTaskFile", "path": "tasks/b-task.md"},
            ],
            "changed_files": [],
            "validation_evidence": {},
        }
    write_output("01-healthz-task.md", derived_task("healthz", "# Implement healthz endpoint", health_task_body()))
    return {
        "submission_id": "breakdown",
        "attempt": attempt,
        "summary": "implementation tasks derived by scripted worker",
        "artifacts": [
            {"type": "ImplementationTaskFile", "path": "01-healthz-task.md"},
        ],
        "changed_files": [],
        "validation_evidence": {},
    }


def implement_payload(attempt):
    repair_prompt = "# Open Tulid Repair" in prompt
    show_batch = is_fault("batch")

    # fault: worker writes app.py but the first attempt omits changed_files (an
    # Acceptance-level requirement), then a repair supplies the exact paths.
    if is_fault("fault_omit_changed_files"):
        (workspace / "app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")
        (workspace / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
        return {
            "submission_id": "implement-omitted",
            "attempt": attempt,
            "summary": "deliberately omitted changed paths",
            "artifacts": [], "changed_files": [],
            "validation_evidence": validation_evidence(),
        }, None

    # fault: worker deletes a tracked repository file and delivers the
    # deletion as an explicit deleted path (plan 5 delivery of deletions).
    if is_fault("fault_delete_file"):
        (workspace / "legacy-note.txt").unlink(missing_ok=True)
        (workspace / "app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")
        return {
            "submission_id": "implement-delete",
            "attempt": attempt,
            "summary": "worker deleted the legacy file",
            "artifacts": [],
            "changed_files": ["app.py", "legacy-note.txt"],
            "validation_evidence": validation_evidence(),
        }, None

    # fault: the worker dies during submission without an accepted completion;
    # the runtime must preserve evidence and schedule a fresh bounded attempt.
    if is_fault("fault_worker_death"):
        marker = workspace / "worker-death-attempts.txt"
        count = int(marker.read_text(encoding="utf-8").strip() or 0) if marker.exists() else 0
        total = int(os.environ.get("SCRIPTED_FAULT_WORKER_DEATHS", "1"))
        if count < total:
            marker.write_text(str(count + 1), encoding="utf-8")
            print(f"worker-death start count={count + 1}")
            sys.exit(9)
        (workspace / "app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")
        return {
            "submission_id": "implement-after-worker-death",
            "attempt": attempt,
            "summary": "implementation after a worker died during submission",
            "artifacts": [],
            "changed_files": ["app.py"],
            "validation_evidence": validation_evidence(),
        }, None

    # fault: worker ends bounded and explicitly blocked because a required
    # frozen context artifact was not supplied; evidence is retained.
    if is_fault("fault_missing_context"):
        output.mkdir(parents=True, exist_ok=True)
        (output / "blocker.md").write_text(
            "# Blocked: missing frozen context\n\nRequired implementation spec was not supplied to the worker.\n",
            encoding="utf-8",
        )
        return {
            "submission_id": "implement-missing-context",
            "attempt": attempt,
            "summary": "blocked: missing frozen context for implementation",
            "artifacts": [{"type": "ImplementationTaskFile", "path": "blocker.md"}],
            "changed_files": [],
            "validation_evidence": validation_evidence(),
        }, None

    # fault: the worker pushes a change onto a path that is not part of the
    # frozen repo baseline, so delivery must refuse to promote an out-of-scope
    # target and repair the submission to stay inside the task's repo surface.
    if is_fault("fault_stale_target"):
        if not repair_prompt:
            (workspace / "moved-target.txt").write_text(
                "value outside the frozen baseline\n", encoding="utf-8"
            )
            (workspace / "app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")
            return {
                "submission_id": "implement-stale-rejected",
                "attempt": attempt,
                "summary": "implementation targets a moved file",
                "artifacts": [],
                "changed_files": ["app.py", "moved-target.txt"],
                "validation_evidence": validation_evidence(),
            }, None
        (workspace / "moved-target.txt").unlink(missing_ok=True)
        (workspace / "app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")
        return {
            "submission_id": "implement-stale-repaired",
            "attempt": attempt,
            "summary": "out-of-scope target dropped after rejection",
            "artifacts": [],
            "changed_files": ["app.py"],
            "validation_evidence": validation_evidence(),
        }, None

    if show_batch:
        (workspace / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (workspace / "app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")
    return {
        "submission_id": "implement-task",
        "attempt": attempt,
        "summary": "implementation completed by scripted worker",
        "artifacts": [],
        "changed_files": ["app.py"] + (["helper.py"] if show_batch else []),
        "validation_evidence": validation_evidence(),
    }, None


def review_payload(attempt):
    if is_fault("fault_review_no_change") or is_fault("self_review_no_change") or is_fault("fault"):
        return {
            "submission_id": "self-review-no-change",
            "attempt": attempt,
            "summary": "self review found no in-scope defect",
            "artifacts": [],
            "changed_files": [],
            "validation_evidence": validation_evidence(),
            "review_result": review_result(),
        }
    append_workspace_file("app.py", "\n# self review\n")
    return {
        "submission_id": "self-review",
        "attempt": attempt,
        "summary": "self review completed by scripted worker",
        "artifacts": [],
        "changed_files": ["app.py"],
        "validation_evidence": validation_evidence(),
        "review_result": {
            "behavior": "health endpoint returns expected response",
            "evidence": "app.py handler and check_repo.py verify the behavior",
            "defects_fixes": [{"defect": "missing blank line", "fix": "added newline", "status": "fixed"}],
            "remaining_blockers": [],
        },
    }


# ---------------------------------------------------------------------------
# Legacy exact transitions (existing scenarios rely on these exact behaviors).
# ---------------------------------------------------------------------------
def legacy():
    if transition_id == "DraftDirection":
        payload = direction_payload()
        status = submit(payload)
        sys.exit(0 if status == 200 else 1)
        return

    if transition_id == "WriteImplementationSpec":
        payload = spec_payload()
        status = submit(payload)
        sys.exit(0 if status == 200 else 1)
        return

    if transition_id == "BreakDownImplementationSpec":
        payload = breakdown_payload()
        status = submit(payload)
        sys.exit(0 if status == 200 else 1)
        return

    if transition_id == "ImplementTask":
        if scenario in ("implementation_feedback_repair", "standard_contract"):
            repair_prompt = "# Open Tulid Repair" in prompt
            forbidden = workspace / "forbidden-by-first-attempt.txt"
            if not repair_prompt:
                forbidden.write_text("remove me during repair\n", encoding="utf-8")
                status = submit({
                    "submission_id": "implement-task-rejected",
                    "attempt": 1,
                    "summary": "deliberately rejected scoped attempt",
                    "artifacts": [],
                    "changed_files": ["forbidden-by-first-attempt.txt"],
                    "validation_evidence": {
                        "tests_pass": "not run; verifier feedback required",
                        "project_build": "not run; verifier feedback required",
                    },
                })
                sys.exit(1 if status == 400 else 2)
            assert forbidden.exists(), "repair must resume the rejected workspace"
            forbidden.unlink()
        (workspace / "app.py").write_text(
            "def healthz():\n    return 'ok'\n",
            encoding="utf-8",
        )
        status = submit({
            "submission_id": (
                "implement-task-repaired"
                if scenario in ("implementation_feedback_repair", "standard_contract")
                else "implement-task"
            ),
            "attempt": 2 if scenario in ("implementation_feedback_repair", "standard_contract") else 1,
            "summary": (
                "implementation task corrected after verifier feedback"
                if scenario != "default" else "implementation task completed by scripted worker"
            ),
            "artifacts": [],
            "changed_files": ["app.py"],
            "validation_evidence": {
                "tests_pass": "passed",
                "project_build": "passed",
            },
        })
        sys.exit(0 if status == 200 else 1)
        return

    if transition_id == "SelfReview":
        payload = review_payload(1)
        status = submit(payload)
        sys.exit(0 if status == 200 else 1)
        return

    print(f"unsupported transition: {transition_id}", file=sys.stderr)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Role-based generic worker: supports renamed states/task types, a generated
# task batch, dependent execution, and arbitrary distinct worker assignments.
# ---------------------------------------------------------------------------
def generic():
    role = role_of(transition_id)
    repair_prompt = "# Open Tulid Repair" in prompt
    attempt = 2 if repair_prompt else 1

    if role == "direction":
        payload = direction_payload(attempt=attempt)
    elif role == "spec":
        payload = spec_payload(attempt=attempt)
    elif role == "breakdown":
        payload = breakdown_payload(attempt=attempt)
    elif role == "implement":
        payload, _ = implement_payload(attempt)
        if payload is None:
            print(f"unsupported implement handling for scenario={scenario}", file=sys.stderr)
            sys.exit(2)
    elif role == "review":
        payload = review_payload(attempt)
    else:
        print(f"unsupported transition: {transition_id}", file=sys.stderr)
        sys.exit(2)

    status = submit(payload)
    sys.exit(0 if status == 200 else 1)


# Renamed chains and injected-fault scenarios use the generic deterministic
# driver; the classic local-worker scenarios keep their exact legacy behaviors
# so existing scripted e2e coverage stays stable.
if is_fault("chain") or (
    transition_id
    in ("ImplementTask", "SelfReview", "DraftDirection", "WriteImplementationSpec", "BreakDownImplementationSpec")
    and (is_fault("fault_") or is_fault("batch") or is_fault("worker_death"))
):
    generic()
else:
    legacy()
