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
scenario = os.environ.get("MOCK_AGENT_SCENARIO", "accept_first_try")
endpoint = os.environ["OPEN_TULID_COMPLETION_ENDPOINT"]
token = os.environ["OPEN_TULID_COMPLETION_TOKEN"]
output = pathlib.Path(os.environ["OPEN_TULID_OUTPUT_DIR"])

print(f"scripted worker scenario={scenario}")
print(f"job={context['job_id']} task={context['task_id']}")
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


if scenario == "no_completion":
    print("exiting without completion")
    sys.exit(0)

if scenario == "reject_then_fix":
    submit({
        "submission_id": "first",
        "attempt": 1,
        "summary": "not done yet",
        "artifacts": [],
        "changed_files": [],
        "validation_evidence": {"tests_pass": "not run"},
    })

if scenario == "missing_artifact_then_fix":
    submit({
        "submission_id": "missing-artifact",
        "attempt": 1,
        "summary": "submitted before artifact file existed",
        "artifacts": [
            {"type": "ImplementationSummary", "path": "implementation-summary.md"},
        ],
        "changed_files": [],
        "validation_evidence": {"tests_pass": "passed"},
    })

output.mkdir(parents=True, exist_ok=True)
(output / "implementation-summary.md").write_text(
    "# Implementation Summary\n\nScripted docker worker completed the task.\n",
    encoding="utf-8",
)
(output / "test-result.md").write_text(
    "# Test Result\n\npytest passed in scripted docker worker.\n",
    encoding="utf-8",
)
(workspace / "app.py").write_text("def healthz():\n    return 'ok'\n", encoding="utf-8")

status = submit({
    "submission_id": "accepted",
    "attempt": 2 if scenario in {"reject_then_fix", "missing_artifact_then_fix"} else 1,
    "summary": "implemented by scripted docker worker",
    "artifacts": [
        {"type": "ImplementationSummary", "path": "implementation-summary.md"},
        {"type": "TestResult", "path": "test-result.md"},
    ],
    "changed_files": ["app.py"],
    "validation_evidence": {"tests_pass": "passed"},
})
sys.exit(0 if status == 200 else 1)
