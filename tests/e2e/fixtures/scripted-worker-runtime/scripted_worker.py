#!/usr/bin/env python3
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
scenario = os.environ.get("SCRIPTED_RUNTIME_SCENARIO", "default")

print(f"scripted runtime worker scenario={scenario} transition={transition_id} task={task_id}")
print(f"prompt-bytes={len(prompt.encode('utf-8'))}")


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
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            print(f"completion status={response.status} body={body}")
            return response.status
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        print(f"completion status={exc.code} body={body}")
        return exc.code


def write_output(name, content):
    output.mkdir(parents=True, exist_ok=True)
    (output / name).write_text(content, encoding="utf-8")


def append_workspace_file(name, text):
    path = workspace / name
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(existing + text, encoding="utf-8")


if transition_id == "DraftDirection":
    write_output(
        "product-spec.md",
        "# Product Spec\n\n## Problem\nCapture clipboard audio and speech reliably.\n\n## Requirements\nProvide a tray-triggered transcription flow.\n",
    )
    write_output(
        "technical-direction.md",
        "# Technical Direction\n\n## Architecture\nUse a small local service and a clipboard bridge.\n\n## Interfaces\nExpose a single transcription entrypoint.\n",
    )
    status = submit({
        "submission_id": "draft-direction",
        "attempt": 1,
        "summary": "direction drafted by scripted worker",
        "artifacts": [
            {"type": "ProductSpec", "path": "product-spec.md"},
            {"type": "TechnicalDirection", "path": "technical-direction.md"},
        ],
        "changed_files": [],
        "validation_evidence": {},
    })
    sys.exit(0 if status == 200 else 1)

if transition_id == "WriteImplementationSpec":
    write_output(
        "implementation-spec.md",
        "# Implementation Spec\n\n## Modules\nAdd clipboard capture, speech orchestration, and result insertion.\n\n## Testing\nUse deterministic repo checks for the E2E workflow.\n",
    )
    status = submit({
        "submission_id": "implementation-spec",
        "attempt": 1,
        "summary": "implementation spec drafted by scripted worker",
        "artifacts": [
            {"type": "ImplementationSpec", "path": "implementation-spec.md"},
        ],
        "changed_files": [],
        "validation_evidence": {},
    })
    sys.exit(0 if status == 200 else 1)

if transition_id == "BreakDownImplementationSpec":
    write_output(
        "01-healthz-task.md",
        (
            "---\n"
            "local_id: healthz\n"
            "---\n"
            "# Implement healthz endpoint\n\n"
            "Add a concrete health check entrypoint and preserve clipboard-ready output flow.\n"
        ),
    )
    status = submit({
        "submission_id": "breakdown",
        "attempt": 1,
        "summary": "implementation tasks derived by scripted worker",
        "artifacts": [
            {"type": "ImplementationTaskFile", "path": "01-healthz-task.md"},
        ],
        "changed_files": [],
        "validation_evidence": {},
    })
    sys.exit(0 if status == 200 else 1)

if transition_id == "ImplementTask":
    (workspace / "app.py").write_text(
        "def healthz():\n    return 'ok'\n",
        encoding="utf-8",
    )
    status = submit({
        "submission_id": "implement-task",
        "attempt": 1,
        "summary": "implementation task completed by scripted worker",
        "artifacts": [],
        "changed_files": ["app.py"],
        "validation_evidence": {
            "tests_pass": "passed",
            "project_build": "passed",
        },
    })
    sys.exit(0 if status == 200 else 1)

if transition_id == "SelfReviewPass1":
    append_workspace_file("app.py", "\n# self review pass 1\n")
    if scenario == "self_review_reject_once":
        rejected = submit({
            "submission_id": "self-review-1-missing-changed-files",
            "attempt": 1,
            "summary": "self review pass 1 omitted changed files first",
            "artifacts": [],
            "changed_files": [],
            "validation_evidence": {
                "tests_pass": "passed",
                "project_build": "passed",
            },
        })
        if rejected == 200:
            print("expected missing changed_files submission to be rejected", file=sys.stderr)
            sys.exit(1)
    status = submit({
        "submission_id": "self-review-1",
        "attempt": 2 if scenario == "self_review_reject_once" else 1,
        "summary": "self review pass 1 completed by scripted worker",
        "artifacts": [],
        "changed_files": ["app.py"],
        "validation_evidence": {
            "tests_pass": "passed",
            "project_build": "passed",
        },
    })
    sys.exit(0 if status == 200 else 1)

if transition_id == "SelfReviewPass2":
    append_workspace_file("app.py", "# self review pass 2\n")
    status = submit({
        "submission_id": "self-review-2",
        "attempt": 1,
        "summary": "self review pass 2 completed by scripted worker",
        "artifacts": [],
        "changed_files": ["app.py"],
        "validation_evidence": {
            "tests_pass": "passed",
            "project_build": "passed",
        },
    })
    sys.exit(0 if status == 200 else 1)

print(f"unsupported transition: {transition_id}", file=sys.stderr)
sys.exit(2)
