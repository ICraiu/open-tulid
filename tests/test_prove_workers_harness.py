"""Plan 6F: regression checks for the prove-configured-workers isolation and
run-ledger harness (examples/wealthy-scholar-verification/prove_workers)."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

MODULE = (Path(__file__).resolve().parents[1] / "examples"
          / "wealthy-scholar-verification" / "prove_workers" / "prove_workers.py")
spec = importlib.util.spec_from_file_location("prove_workers", MODULE)
pw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pw)

PHASES = ["planning", "implementation", "review", "delivery"]


def isolated_runner(targets, fail_first_task=None):
    """Deterministic runner: fails implementation once for one task, repairs it."""
    def runner(task, phase, targets, retry_index):
        if phase == "implementation":
            if task["id"] == fail_first_task and retry_index == 0:
                return {"status": "failure", "evidence": ["omitted requirement"],
                        "duration_seconds": 0.1, "manual": False}
            return {"status": "ok", "evidence": [], "duration_seconds": 0.05,
                    "manual": False}
        if phase == "delivery":
            if task["id"] == fail_first_task and retry_index == 0:
                return {"status": "blocked",
                        "evidence": ["delivery blocked: implementation not accepted"],
                        "duration_seconds": 0.0, "manual": False}
            (targets["workspace"] / f"delivered-{task['id']}.txt").write_text("delivered\n")
            return {"status": "delivered", "evidence": [f"delivered {task['id']}"],
                    "duration_seconds": 0.1, "manual": False}
        return {"status": "ok", "evidence": [], "duration_seconds": 0.05, "manual": False}
    return runner


class SanitizeTests(unittest.TestCase):
    def test_secrets_are_redacted(self):
        dirty = "token: sk-abc0123456789abcdef  api_key=Jan9AK1111111111111111  secret=junk"
        out = pw.sanitize(dirty)
        for secret in ("sk-abc", "Jan9AK", "junk"):
            self.assertNotIn(secret, out)
        self.assertIn("<redacted>", out)


class ShieldTests(unittest.TestCase):
    def test_live_root_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            live_repo = Path(tmp) / "live-project"
            live_tracker = Path(tmp) / "live-vault"
            workspace = Path(tmp) / "experiment" / "workspace"
            workspace.mkdir(parents=True)
            roots = {"repo": live_repo, "tracker": live_tracker}

            violations = pw.shield([live_repo, workspace], roots, "config")
            self.assertEqual(len(violations), 1)
            self.assertIn("live-project", violations[0])

            clean = pw.shield([workspace], roots, "config")
            self.assertEqual(clean, [])
            self.assertTrue(workspace.is_relative_to(_parent(workspace)))

    def test_no_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            live_repo = Path(tmp) / "live-project"
            link = Path(tmp) / "link-to-live"
            link.symlink_to(live_repo)
            violations = pw.shield([link], {"repo": live_repo}, "config")
            self.assertTrue(violations, "symlinked live root must be rejected")


def _parent(p: Path) -> Path:
    return p.parent


class LedgerTests(unittest.TestCase):
    def test_chain_repair_is_recorded_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            workspace = tmp / "workspace"
            workspace.mkdir()
            ledger = pw.Ledger(tmp / "ledger")
            tasks = [{"id": 0, "name": "task-0", "depends": []},
                     {"id": 1, "name": "task-1", "depends": [0]},
                     {"id": 2, "name": "task-2", "depends": [1]}]
            pw.run_chain(ledger, tasks, isolated_runner({"workspace": workspace}, fail_first_task=1),
                         {"workspace": workspace}, {}, phases=PHASES)
            outputs = ledger.write("-test")
            record = json.loads(Path(outputs["json"]).read_text())

            summary = record["summary"]
            self.assertEqual(summary["completed"], 3)
            self.assertGreaterEqual(summary["failed"], 1)   # failing attempt kept
            self.assertGreaterEqual(summary["retries"], 1)  # repair retry counted

            # The failing attempt must appear; failure is visible, not dropped.
            statuses = [a["status"] for e in record["chain"]
                        for a in e["attempts"] if e["task"]["id"] == 1]
            self.assertIn("failure", statuses)
            self.assertIn("blocked", statuses)

            self.assertTrue(Path(outputs["markdown"]).is_file())
            self.assertIn("shield violations: 0", Path(outputs["markdown"]).read_text())
            self.assertTrue((workspace / "delivered-0.txt").is_file())

            # Chain order preserves dependencies (delivery order = task order).
            delivered = [a["phase"] for e in record["chain"]
                         for a in e["attempts"] if a.get("status") == "delivered"]
            self.assertEqual(len([d for d in delivered if d == "delivery"]), 3)


if __name__ == "__main__":
    unittest.main()
