#!/usr/bin/env python3
"""
reliability_auto_loop.py

Automates the manual reliability-plan loop:

  every N seconds:
    - if the opencode run for the current task is still going  -> log, sleep
    - if it finished (or never started):
        - compare the last commit message against the issued task code
        - match            -> success: restart model, launch next task
        - no match / other -> failure: log reason and stop the program

State is persisted in the config's state_file so the loop can be
restarted without losing position or re-running tasks.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "reliability_auto_config.json"


# --------------------------------------------------------------------------- helpers

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Runner:
    """Logs to stdout and to a file."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_fh = open(log_path, "a", buffering=1, encoding="utf-8")

    def log(self, msg):
        line = f"[{now_iso()}] {msg}"
        print(line, flush=True)
        self.log_fh.write(line + "\n")
        self.log_fh.flush()

    def close(self):
        self.log_fh.close()


def load_config(path=CONFIG_PATH):
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg["repo_dir"] = str((Path(path).resolve().parent / Path(cfg["repo_dir"]).expanduser()).resolve())
    cfg["state_file"] = str(Path(cfg["repo_dir"]) / cfg["state_file"])
    cfg["log_file"] = str(Path(cfg["repo_dir"]) / cfg["log_file"])
    for plan, rel in cfg["plan_files"].items():
        cfg["plan_files"][plan] = str(Path(cfg["repo_dir"]) / rel)
    return cfg


def load_state(cfg):
    sf = Path(cfg["state_file"])
    if sf.exists():
        with open(sf, encoding="utf-8") as fh:
            return json.load(fh)
    return {"issued": None, "next_index": 0, "running_pid": None,
            "failed": False, "last_error": None}


def save_state(cfg, state):
    state["updated_at"] = now_iso()
    tmp = Path(cfg["state_file"]).with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, Path(cfg["state_file"]))


def process_info(pid):
    """Linux process state and identity; start time protects against PID reuse."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return fields[0], f"{boot}:{fields[19]}"
    except FileNotFoundError:
        return None


def pid_alive(pid, identity=None):
    if not pid:
        return False
    # Reap our child if it exited; kill(pid, 0) alone sees zombies as alive.
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass  # A worker from a previous invocation is not our child.
    info = process_info(pid)
    return bool(info and info[0] not in {"Z", "X"}
                and (identity is None or info[1] == identity))


def active_opencode(cfg):
    """Wait for other OpenCode processes too: ds4ctl restarts a shared model."""
    names = {"opencode", Path(cfg["open_code_bin"]).name}
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if comm not in names and not any(
                Path(os.fsdecode(arg)).name in names for arg in argv[:2] if arg
            ):
                continue
            if pid_alive(int(entry.name)):
                found.append(entry.name)
        except FileNotFoundError:
            continue
        except PermissionError:
            continue
    return found


def run_cmd(cmd, cfg, timeout=60):
    try:
        proc = subprocess.run(cmd, cwd=cfg["repo_dir"], capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode == 0, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired as exc:
        return False, "", f"command timed out: {exc}"
    except FileNotFoundError as exc:
        return False, "", f"command not found: {exc}"


def last_commit_message(cfg):
    proc = subprocess.run(["git", "-C", cfg["repo_dir"], "log", "-1", "--format=%s%n%b"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed: {proc.stderr.strip()}")
    return (proc.stdout or "").strip()


def commit_matches(message, code):
    if not message:
        return False
    return re.search(rf"\b{re.escape(code)}\b", message) is not None


def find_step_heading(cfg, code, plan_file):
    """Return the '### <code>. <title>' line for a step, or None."""
    if not plan_file or not Path(plan_file).exists():
        return None
    try:
        lines = Path(plan_file).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, IsADirectoryError):
        return None
    pattern = re.compile(rf"^###\s+{re.escape(code)}(?:\.\s+|\s+|$)(.*)$")
    for line in lines:
        m = pattern.match(line.strip())
        if m:
            title = m.group(1).strip()
            return f"### {code}. {title}" if title else f"### {code}."
    return None


def build_prompt(cfg, code):
    plan_num = re.match(r"\d+", code).group()
    plan_file = cfg["plan_files"].get(plan_num)
    if not plan_file or not Path(plan_file).exists():
        return None
    heading = find_step_heading(cfg, code, plan_file)
    if heading is None:
        return None
    task_label = heading.lstrip("# ").strip()
    return (
        f"Read the master plan file: docs/reliability-polish-plan.md.\n"
        f"Then read the individual plan file: {plan_file}.\n"
        f"From that plan, focus solely on task {task_label} and nothing else.\n\n"
        f"Working directory: {cfg['repo_dir']}.\n\n"
        f"{cfg.get('prompt_context', '')}\n\n"
        f"When done, commit the result in this repo with a message that includes the "
        f"code {code}, e.g. \"implement plan {plan_num}, step {code}\".\n"
        f"Before committing, run the project verification commands documented in "
        f"docs/implementation-contracts.md and the acceptance checks relevant to this task. "
        f"Report the observed behavior and evidence. Leave unrelated user changes out of the commit."
    )


def restart_model(cfg, runner):
    if not cfg["restart_model"]:
        return True
    runner.log("restarting model: ds4ctl stop ...")
    ok, so, se = run_cmd(["ds4ctl", "stop"], cfg)
    if not ok:
        runner.log(f"ds4ctl stop failed: {se or so}")
        return False
    time.sleep(3)
    runner.log("restarting model: ds4ctl start ...")
    ok, so, se = run_cmd(["ds4ctl", "start"], cfg, timeout=120)
    if not ok:
        runner.log(f"ds4ctl start failed: {se or so}")
        return False
    runner.log("model restarted")
    return True


def launch_open_code(cfg, runner, code, prompt):
    cmd = [cfg["open_code_bin"], "run"]
    if cfg.get("allow_auto_approve"):
        cmd.append("--auto")
    if cfg.get("model"):
        cmd += ["-m", cfg["model"]]
    cmd.append(prompt)

    task_log = Path(cfg["repo_dir"]) / f".reliability-task-{code}.log"
    log_fh = open(task_log, "a", encoding="utf-8")
    runner.log(f"launching opencode for task {code}: {' '.join(cmd[0:2])} ... (log {task_log.name})")
    with log_fh:
        proc = subprocess.Popen(cmd, cwd=cfg["repo_dir"], stdin=subprocess.DEVNULL,
                                stdout=log_fh, stderr=subprocess.STDOUT,
                                start_new_session=True)
    # Keep the Popen object alive until the runner exits.
    runner.child = proc
    return proc.pid, task_log


def tail_task_log(task_log, n=3):
    try:
        if not task_log:
            return ""
        with Path(task_log).open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
        lines = [l for l in lines if l.strip()]
        return "\n".join(lines[-n:][-min(n, len(lines)):]) if lines else ""
    except (OSError, ValueError):
        return ""


def issue_task(cfg, runner, state, code):
    prompt = build_prompt(cfg, code)
    if prompt is None:
        fail(cfg, runner, state, f"invalid task code or missing plan heading: {code}")
    if not restart_model(cfg, runner):
        fail(cfg, runner, state, f"model restart failed before task {code}")
    pid, task_log = launch_open_code(cfg, runner, code, prompt)
    info = process_info(pid)
    state["process_identity"] = info[1] if info else None
    state["issued"] = code
    state["running_pid"] = pid
    state["_task_log"] = str(task_log)
    state["next_index"] += 1
    save_state(cfg, state)
    runner.log(f">>> issued task {code} (pid {pid}), next index {state['next_index']}")
    return True


def confirm_and_advance(cfg, runner, state):
    code = state["issued"]
    runner.log(f"opencode run finished for task {code}; checking last commit ...")
    message = last_commit_message(cfg)
    runner.log(f"last commit: {re.sub(chr(10), ' | ', message)}")

    if commit_matches(message, code):
        runner.log(f"SUCCESS: commit contains task code {code}")
        state["issued"] = None
        state["running_pid"] = None
        state["_task_log"] = None
        save_state(cfg, state)
        next_code = next_task(state, cfg)
        if next_code:
            issue_task(cfg, runner, state, next_code)
        else:
            runner.log("ALL TASKS COMPLETE — no tasks remaining.")
            state["failed"] = None  # sentinel: finished normally
            save_state(cfg, state)
            raise SystemExit(0)
        return
    fail(cfg, runner, state,
         f"task {code} did not produce a commit containing its code; "
         f"last commit = {message!r}")


def next_task(state, cfg):
    order = cfg["order"]
    idx = state.get("next_index", 0)
    return order[idx] if idx < len(order) else None


def valid_step(cfg, code):
    match = re.fullmatch(r"(\d+)[A-Z]+", code)
    plan_file = cfg["plan_files"].get(match[1]) if match else None
    return bool(plan_file and find_step_heading(cfg, code, plan_file))


def fail(cfg, runner, state, reason):
    runner.log(f"FAILURE: {reason}")
    runner.log("Stopping the program (no more tasks will be issued).")
    state["failed"] = True
    state["last_error"] = reason
    save_state(cfg, state)
    raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--start-at", help="Explicitly reset saved progress to this task code")
    parser.add_argument("--dry-run", action="store_true", help="Show queue and prompt without running commands or changing state")
    args = parser.parse_args()
    cfg = load_config(args.config)
    if cfg["check_interval_seconds"] <= 0:
        parser.error("check_interval_seconds must be positive")
    for code in cfg["order"]:
        if not valid_step(cfg, code):
            parser.error(f"unknown task or missing plan heading: {code}")
    if args.start_at and args.start_at not in cfg["order"]:
        parser.error("--start-at must be a code in the configured order")
    if args.dry_run:
        state = load_state(cfg)
        idx = cfg["order"].index(args.start_at) if args.start_at else state["next_index"]
        print("Queue:", ", ".join(cfg["order"][idx:]))
        code = args.start_at or state.get("issued") or next_task(state, cfg)
        if code:
            print(build_prompt(cfg, code))
        return

    # Keep this descriptor open for the lifetime of the loop.
    lock = open(cfg["state_file"] + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("another loop is already using this state file")
    runner = Runner(Path(cfg["log_file"]))
    runner.log("=== reliability_auto_loop starting ===")
    runner.log(f"repo={cfg['repo_dir']}, order={cfg['order']!r}, interval={cfg['check_interval_seconds']}s")

    state = load_state(cfg)
    try:
        if args.start_at:
            if pid_alive(state.get("running_pid"), state.get("process_identity")) or active_opencode(cfg):
                runner.log("Cannot reset progress while OpenCode is running.")
                raise SystemExit(1)
            state = {"issued": None, "running_pid": None, "failed": False,
                     "last_error": None, "next_index": cfg["order"].index(args.start_at)}
            save_state(cfg, state)
        if state.get("failed"):
            runner.log(f"previous state is failed: {state.get('last_error')}")
            raise SystemExit(1)

        idx = state.get("next_index", 0)
        if not (0 <= idx < len(cfg["order"])) and not state.get("issued"):
            # nothing left to do
            runner.log("order exhausted; nothing to run.")
            raise SystemExit(0)

        first = True
        while True:
            if not first:
                time.sleep(cfg["check_interval_seconds"])
            first = False

            issued = state.get("issued")
            pid = state.get("running_pid")

            if issued and pid_alive(pid, state.get("process_identity")):
                tail = tail_task_log(state.get("_task_log"))
                runner.log(f"opencode still running task {issued} (pid {pid})"
                           + (f"\n   latest: {tail}" if tail else ""))
                continue

            busy = active_opencode(cfg)
            if busy:
                runner.log(f"OpenCode running (pids {', '.join(busy)}); waiting.")
                continue

            if issued:
                confirm_and_advance(cfg, runner, state)
                continue

            # nothing issued -> start the next task (immediate on first pass)
            next_code = next_task(state, cfg)
            if next_code is None:
                runner.log("ALL TASKS COMPLETE.")
                save_state(cfg, state)
                raise SystemExit(0)
            outcome = issue_task(cfg, runner, state, next_code)
            if outcome is None:
                runner.log("no remaining valid tasks.")
                raise SystemExit(0)
            if not outcome:
                fail(cfg, runner, state, "failed to issue next task")
    except (OSError, ValueError, RuntimeError) as exc:
        fail(cfg, runner, state, str(exc))
    except KeyboardInterrupt:
        save_state(cfg, state)
        runner.log("interrupted; state saved. Active OpenCode continues; resume by running again.")
        raise SystemExit(130)
    finally:
        runner.close()
        lock.close()


if __name__ == "__main__":
    main()
