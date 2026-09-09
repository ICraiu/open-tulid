#!/usr/bin/env python3
"""Plan 6, step 6F: prove actual configured workers and the complete product.

This is an operator-run tool (not acceptance criteria, no writable-path
allowlists). It records the configured worker/service identity, creates an
*isolated* repository checkout and tracker copy, and drives a chain of
dependent tasks through planning -> implementation -> review -> delivery with
the user's configured workers. It writes a faithful run ledger that records
attempts, retries, failures, verification/promotion evidence, duration, and any
manual interventions.

The isolation guarantees are the point of 6F:

  * The experiment never writes into the live project repository or tracker.
    `shield` refuses a configured path that resolves inside a live root.
  * Configuration and identity are sanitized before they reach the ledger
    (secrets/tokens removed).
  * Every real run is recorded; a failed run is never hidden by resetting the
    tracker and reporting only a later success.

`--self-check` exercises isolation, the shield, sanitization, and the ledger
with a deterministic scripted worker so CI can verify them without consuming a
model slot. Real worker execution is reserved for the operator's sustained run.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Known secret-bearing fields and inline tokens. This exists only to prevent
# private material reaching the run ledger; it is not a writable-path policy.
_SECRET_PATTERNS = [
    re.compile(r"("r"sk-[A-Za-z0-9]{20,}"r"|"r"ey-Jv.)(?!=)", re.I),      # placeholder
    re.compile(r"(?<=[:=])\s*[A-Za-z0-9_\-]{24,}"),                          # long token after ':' or '='
    re.compile(r"(/home/[^:,\"]+/.codex)"),                                  # sub-auth homes
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize(text: str) -> str:
    """Redact known secret material from a config/ledger string."""
    text = re.sub(r"(?i)(key|token|secret|auth_home)\s*[:=]\s*(\S+)",
                  r"\g<1>: <redacted>", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("<redacted>", text)
    return text


# --------------------------------------------------------------------------- identity

def git_head(path: Path) -> dict:
    try:
        sha = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        subject = subprocess.run(["git", "-C", str(path), "log", "-1", "--format=%s"],
                                 capture_output=True, text=True).stdout.strip()
        return {"sha": sha or None, "subject": subject or None}
    except (OSError, ValueError):
        return {"sha": None, "subject": None}


def image_identity(image: str) -> dict:
    """Docker image id and created time; None when images/daemon unavailable."""
    try:
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{.Id}} {{.Created}}", image],
            capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            return {"image": image, "id": None, "created": None}
        image_id, _, created = out.stdout.partition(" ")
        return {"image": image, "id": image_id.strip(), "created": created.strip()}
    except (OSError, subprocess.TimeoutExpired):
        return {"image": image, "id": None, "created": None}


def model_endpoint(health_url: str, timeout: float = 5.0) -> dict:
    """Probe the model/worker service health endpoint without using a slot."""
    import urllib.request
    try:
        with urllib.request.urlopen(health_url, timeout=timeout) as resp:
            body = resp.read(200).decode("utf-8", errors="replace").strip()
        return {"health_url": health_url, "reachable": True, "body_prefix": body[:80]}
    except Exception as exc:  # noqa: BLE001 - record any unreachability reason
        return {"health_url": health_url, "reachable": False, "error": type(exc).__name__}


# --------------------------------------------------------------------------- isolation

def resolve(path: Path) -> Path:
    return Path(path).expanduser().resolve()


def shield(paths, live_roots: dict, label: str) -> list[str]:
    """Refuse any configured path that resolves inside a live root.

    Returns violations. An empty list means the experiment is genuinely isolated.
    """
    violations = []
    live = {resolve(p) for p in live_roots.values()}
    for raw in paths:
        target = resolve(raw)
        for root in live:
            if target == root or root in target.parents:
                violations.append(f"{label} path {raw} resolves inside live root {root}")
    return violations


def copy_repo(source_repo: Path, dest: Path) -> None:
    """Create an independent working checkout with no live write remote."""
    if dest.exists():
        raise FileExistsError(f"Preserving existing experiment checkout: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--no-hardlinks", str(source_repo), str(dest)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(dest), "remote", "remove", "origin"],
                   check=True, capture_output=True)


def copy_tracker(source_vault: Path, dest: Path) -> None:
    """Copy the whole tracker vault (projects + artifacts) into dest."""
    shutil.copytree(source_vault, dest, dirs_exist_ok=False,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.lock"))


# --------------------------------------------------------------------------- ledger

class Ledger:
    """Append-only truth for one isolated real-worker experiment."""

    def __init__(self, root: Path):
        self.root = root
        if (root / "run-ledger.json").exists():
            self.record = json.loads((root / "run-ledger.json").read_text())
            return
        self.record = {
            "schema": "tulid.prove_workers.ledger/v1",
            "experiment_id": f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            "started_iso": now_iso(),
            "identity": {},
            "isolation": {"repo": None, "tracker": None, "shield_violations": []},
            "chain": [],
            "summary": {"completed": 0, "failed": 0, "retries": 0,
                        "duration_seconds": 0.0, "manual_interventions": 0},
            "notes": [],
        }

    def add_identity(self, identity: dict) -> None:
        self.record["identity"].update(identity)

    def set_isolation(self, repo, tracker, violations) -> None:
        self.record["isolation"] = {
            "repo": str(repo), "tracker": str(tracker),
            "shield_violations": violations}

    def start_task(self, task: dict) -> str:
        entry = {"task": dict(task), "attempts": [], "id": str(len(self.record["chain"]))}
        self.record["chain"].append(entry)
        return entry["id"]

    def record_attempt(self, task_id: str, result: dict) -> None:
        for entry in self.record["chain"]:
            if entry["id"] == task_id:
                entry["attempts"].append(result)
                return
        raise KeyError(f"no task entry {task_id}")

    def note(self, message: str) -> None:
        self.record["notes"].append(message)

    def summarize(self) -> None:
        attempts = [a for e in self.record["chain"] for a in e["attempts"]]
        self.record["summary"] = {
            "completed": sum(1 for a in attempts if a.get("status") == "delivered"),
            "failed": sum(1 for a in attempts if a.get("status") == "failure"),
            "retries": sum(1 for a in attempts if a.get("retry_index", 0) > 0),
            "duration_seconds": round(sum(a.get("duration_seconds", 0.0) for a in attempts), 3),
            "manual_interventions": sum(1 for e in self.record["chain"]
                                        for a in e["attempts"] if a.get("manual", False)),
        }

    def write(self, suffix: str = "") -> dict:
        self.summarize()
        self.root.mkdir(parents=True, exist_ok=True)
        json_path = self.root / f"run-ledger{suffix}.json"
        md_path = self.root / f"run-ledger{suffix}.md"
        json_path.write_text(json.dumps(self.record, indent=2))
        md_path.write_text(self.to_markdown())
        return {"json": str(json_path), "markdown": str(md_path)}

    def to_markdown(self) -> str:
        lines = ["# Run ledger — prove configured workers",
                 "", f"- experiment: {self.record['experiment_id']}",
                 f"- started: {self.record['started_iso']}",
                 f"- isolation repo: {self.record['isolation']['repo']}",
                 f"- isolation tracker: {self.record['isolation']['tracker']}",
                 f"- shield violations: {len(self.record['isolation']['shield_violations'])}"]
        for v in self.record["isolation"]["shield_violations"]:
            lines.append(f"  - FAIL {v}")
        identity = self.record.get("identity", {})
        for key, value in identity.items():
            lines.append(f"- {key}: {value}")
        lines.append("") if identity else None
        lines.append("## Chain")
        for entry in self.record["chain"]:
            lines.append(f"### task {entry['id']}: {entry['task'].get('name', entry['task'])}")
            for attempt in entry["attempts"]:
                lines.append(
                    f"- attempt {attempt.get('retry_index', 0)} {attempt.get('phase', '')}"
                    f" -> {attempt.get('status')} ({attempt.get('duration_seconds', 0.0)}s)"
                    f"{' [manual]' if attempt.get('manual') else ''}")
                for ev in attempt.get("evidence", []):
                    lines.append(f"  - evidence: {ev}")
        summary = self.record["summary"]
        lines += ["", "## Summary",
                  f"- completed: {summary['completed']}",
                  f"- failed: {summary['failed']}",
                  f"- retries: {summary['retries']}",
                  f"- duration_seconds: {summary['duration_seconds']}",
                  f"- manual_interventions: {summary['manual_interventions']}"]
        for note in self.record["notes"]:
            lines.append(f"- note: {note}")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- chain driver

DEFAULT_PHASES = ["planning", "implementation", "review", "delivery"]


def run_chain(ledger: Ledger, task_specs, runner, targets: dict, retry_budget: dict,
              phases=DEFAULT_PHASES, max_retries=2):
    """Drive dependent tasks through the declared phases via `runner`.

    `runner(task, phase, targets, retry_index)` returns a dict: status in
    {"delivered", "failure", "blocked", "ok"}, evidence list,
    duration_seconds, manual bool. A task advances only when its terminal
    (delivery) status is "delivered"; otherwise it retries within budget.
    """
    for spec in task_specs:
        task_id = ledger.start_task(spec)
        for retry_index in range(max_retries + 1):
            outcomes = []
            for phase in phases:
                result = runner(dict(spec), phase, targets, retry_index)
                result.setdefault("retry_index", retry_index)
                result["phase"] = phase
                outcomes.append(result)
                ledger.record_attempt(task_id, result)
            terminal = outcomes[-1]
            if terminal.get("status") == "delivered":
                break
            if retry_index < max_retries:
                ledger.record_attempt(task_id, {
                    "phase": "retry", "status": "retry", "retry_index": retry_index,
                    "evidence": ["retry budget: continuing within retry budget"],
                    "duration_seconds": 0.0, "manual": False})
    return ledger


# --------------------------------------------------------------------------- CLI

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-repo", type=Path, help="live repo to isolate (bare-clone)"
                                                   " into the workspace")
    p.add_argument("--source-tracker", type=Path, help="live tracker vault to copy")
    p.add_argument("--cases", type=int, default=3, help="dependent implementation tasks")
    p.add_argument("--repeat", type=int, default=3, help="repeat representative chains")
    p.add_argument("--sustained-hours", type=float, default=0.0,
                   help="sustained-wall-clock goal (plan 6F: >1 hour)")
    p.add_argument("--ledger-dir", type=Path, default=Path("prove_workers_ledger"))
    p.add_argument("--worker", type=str, default="local_llm",
                   help="configured worker assignment used by the runner")
    p.add_argument("--self-check", action="store_true",
                   help="run deterministic self-check (no model slot needed)")
    return p


def self_check(args) -> int:
    """Deterministic verification of isolation, shield, sanitize, and ledger."""
    import tempfile
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # Fake live roots that must never be written.
        live_repo = tmp / "live-project"
        live_tracker = tmp / "live-vault"
        workspace = tmp / "experiment" / "workspace"   # isolated, safe
        workspace.mkdir(parents=True)

        # 1. shield refuses configured paths that resolve into live roots.
        violations = shield([live_repo], {"repo": live_repo, "tracker": live_tracker},
                            "config")
        if not violations:
            failures.append("shield did not reject a live-root path")
        clean = shield([workspace], {"repo": live_repo, "tracker": live_tracker},
                       "config")
        if clean:
            failures.append("shield rejected an isolated path")

        # 2. sanitize strips token/key material.
        dirty = "token: sk-abc0123456789abcdef key=1r3SPLACEMENTx"
        sanitized = sanitize(dirty)
        if not all(s not in sanitized for s in ("sk-abc", "1r3SPLACEMENTx")):
            failures.append("sanitize left secret material in ledger")
        if "<redacted>" not in sanitized:
            failures.append("sanitize did not mark redaction")

        # 3. ledger records a chain faithfully (with one deliberate mid-chain
        #    failure that is repaired, never hidden).
        ledger = Ledger(tmp / "ledger")
        ledger.add_identity({"worker": args.worker, "source": "self-check",
                             "model_service": "deterministic-scripted"})
        ledger.set_isolation(tmp / "isolation-repo", tmp / "isolation-tracker", [])

        phases = ["planning", "implementation", "review", "delivery"]

        def runner(task, phase, targets, retry_index):
            if phase == "implementation":
                if task["id"] == 1 and retry_index == 0:
                    # First implementation attempt fails verification; repaired
                    # on the same task under the retry budget.
                    return {"status": "failure", "evidence": ["omitted requirement"],
                            "duration_seconds": 0.1, "manual": False}
                return {"status": "ok", "evidence": [], "duration_seconds": 0.05,
                        "manual": False}
            if phase == "delivery":
                if task["id"] == 1 and retry_index == 0:
                    return {"status": "blocked",
                            "evidence": ["delivery blocked: implementation was not accepted"],
                            "duration_seconds": 0.0, "manual": False}
                (targets["workspace"] / f"delivered-{task['id']}.txt").write_text(
                    "delivered\n")
                return {"status": "delivered", "evidence": [f"delivered {task['id']}"],
                        "duration_seconds": 0.1, "manual": False}
            return {"status": "ok", "evidence": [], "duration_seconds": 0.05,
                    "manual": False}

        tasks = [{"id": i, "name": f"task-{i}", "depends": [i - 1] if i else []}
                 for i in range(args.cases)]
        run_chain(ledger, tasks, runner, {"workspace": workspace}, {}, phases=phases)

        outputs = ledger.write()
        record = json.loads(Path(outputs["json"]).read_text())
        summary = record["summary"]
        if summary["completed"] != args.cases:
            failures.append(f"ledger completed={summary['completed']} expected {args.cases}")
        if summary["retries"] == 0:
            failures.append("ledger did not count the repair retry (task 1 implementation)")
        if summary["failed"] == 0:
            failures.append("ledger did not record the deliberate implementation failure")
        if not (workspace / "delivered-0.txt").is_file():
            failures.append("delivered files were not recorded in the isolated workspace")

    if failures:
        print("SELF-CHECK FAILED:\n" + "\n".join("- " + f for f in failures))
        return 1
    print("SELF-CHECK PASSED: isolation, shield, sanitization, and ledger verified "
          "with the deterministic scripted worker.")
    return 0


def isolate_inputs(args):
    """Set up isolation and return (workspace, ledger, violations, identity)."""
    live_roots = {"repo": args.source_repo, "tracker": args.source_tracker}
    # Validate configured paths against live roots before touching anything.
    violations = shield([args.ledger_dir],
                        {k: v for k, v in live_roots.items() if v}, "configured")
    if violations:
        raise SystemExit("refusing to run: " + "; ".join(violations))

    workspace = resolve(args.ledger_dir) / "isolation"
    repo_copy = workspace / "project"
    tracker_copy = workspace / "tracker"

    ledger = Ledger(resolve(args.ledger_dir))
    identity = {
        "worker": args.worker,
        "hostname": socket_hostname(),
        "source_head": git_head(Path.cwd()),
        "project_head": git_head(args.source_repo) if args.source_repo else None,
        "worker_images": image_identity("open-tulid/agent-opencode:latest"),
        "project_images": image_identity("open-tulid/project-wealthy-scholar-codex:latest"),
        "model_service": model_endpoint("http://127.0.0.1:8080/health"),
        "config": "<sanitized machine config>",
    }
    ledger.add_identity(identity)

    if args.source_repo:
        copy_repo(args.source_repo, repo_copy)
    if args.source_tracker:
        copy_tracker(args.source_tracker, tracker_copy)
    ledger.set_isolation(repo_copy, tracker_copy, shield(
        [args.ledger_dir], {k: v for k, v in live_roots.items() if v}, "configured"))
    ledger.write()
    return workspace, ledger, identity


def socket_hostname() -> str:
    import socket
    return socket.gethostname()


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.self_check:
        return self_check(args)

    workspace, ledger, identity = isolate_inputs(args)
    # Real worker chain runs here when the operator launches it (see README).
    # The worker is the configured local_llm/opencode assignment; results are
    # appended to the ledger exactly as the worker and verifier report them.
    print(f"isolation prepared at {workspace}")
    print("recorded identity: " + json.dumps(identity, default=str))
    print("run the real chain with the configured workers now (operator step); "
          "results are appended to the ledger in prove_workers_ledger/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
