"""Regression checks using isolated repositories and a fake OpenCode executable."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE_SCRIPT = Path(__file__).resolve().parents[1] / 'reliability_auto_loop.py'
SCRIPT = SOURCE_SCRIPT
spec = importlib.util.spec_from_file_location('reliability_auto_loop', SCRIPT)
loop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loop)


class LoopTests(unittest.TestCase):
    def test_saved_string_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'worker.log'
            path.write_text('one\n\ntwo\nthree\nfour\n')
            self.assertEqual(loop.tail_task_log(str(path)), 'two\nthree\nfour')
            self.assertEqual(loop.tail_task_log(None), '')

    def test_exited_child_is_not_running(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        try:
            deadline = time.monotonic() + 5
            while loop.pid_alive(child.pid) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertFalse(loop.pid_alive(child.pid))
        finally:
            child.wait()

    def test_pid_identity(self):
        self.assertTrue(loop.pid_alive(os.getpid()))
        self.assertFalse(loop.pid_alive(os.getpid(), 'old-process'))

    def test_commit_code(self):
        self.assertTrue(loop.commit_matches('implement\n\nstep 3D', '3D'))
        self.assertFalse(loop.commit_matches('step 13D or 3DA', '3D'))

    def test_restart_order_and_failure(self):
        from unittest.mock import Mock
        cfg = {'restart_model': True}
        with patch.object(loop, 'run_cmd', return_value=(True, '', '')) as run, patch.object(loop.time, 'sleep'):
            self.assertTrue(loop.restart_model(cfg, Mock()))
            self.assertEqual([c.args[0] for c in run.call_args_list], [['ds4ctl', 'stop'], ['ds4ctl', 'start']])
        with patch.object(loop, 'run_cmd', return_value=(False, '', 'failed')) as run:
            self.assertFalse(loop.restart_model(cfg, Mock()))
            self.assertEqual(run.call_count, 1)

    def fixture(self, root, success):
        def git(*args):
            subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True)
        git('init')
        git('config', 'user.email', 'test@example.invalid')
        git('config', 'user.name', 'Loop test')
        git('commit', '--allow-empty', '-m', 'baseline')
        (root / 'plan3.md').write_text('### 3D. First\n')
        (root / 'plan4.md').write_text('### 4A. Second\n')
        worker = root / 'fake-opencode'
        worker.write_text('#!' + sys.executable + '\n' + f'''
import re, subprocess, sys, time
code = re.search(r'code (\\d+[A-Z]+)', sys.argv[-1]).group(1)
print('working on ' + code, flush=True)
time.sleep(.2)
if {success!r}:
    subprocess.run(['git', 'commit', '--allow-empty', '-m', 'finished ' + code], check=True)
''')
        worker.chmod(0o755)
        cfg = {'repo_dir': str(root), 'plan_files': {'3': 'plan3.md', '4': 'plan4.md'},
               'order': ['3D', '4A'], 'check_interval_seconds': .03,
               'state_file': 'state.json', 'log_file': 'loop.log',
               'restart_model': False, 'open_code_bin': str(worker)}
        harness = root / 'loop_harness.py'
        harness.write_text(
            'import runpy\n'
            f'namespace = runpy.run_path({str(SOURCE_SCRIPT)!r})\n'
            'namespace["main"].__globals__["active_opencode"] = lambda cfg: []\n'
            'raise SystemExit(namespace["main"]())\n'
        )
        self.script = harness
        config = root / 'config.json'
        config.write_text(json.dumps(cfg))
        return config

    def test_complete_queue_across_plans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.fixture(root, True)
            result = subprocess.run([sys.executable, str(self.script), '--config', str(config)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('still running task 3D', result.stdout)
            self.assertIn('latest: working on 3D', result.stdout)
            self.assertIn('SUCCESS: commit contains task code 4A', result.stdout)
            state = json.loads((root / 'state.json').read_text())
            self.assertIsNone(state['issued'])
            self.assertEqual(state['next_index'], 2)

    def test_failure_stops_and_stays_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.fixture(root, False)
            cmd = [sys.executable, str(self.script), '--config', str(config)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('FAILURE: task 3D', result.stdout)
            self.assertNotIn('issued task 4A', result.stdout)
            self.assertEqual(json.loads((root / 'state.json').read_text())['issued'], '3D')
            resumed = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(resumed.returncode, 1)
            self.assertIn('previous state is failed', resumed.stdout)

    def test_start_at_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self.fixture(root, True)
            cmd = [sys.executable, str(self.script), '--config', str(config), '--start-at', '4A']
            preview = subprocess.run([*cmd, '--dry-run'], capture_output=True, text=True, timeout=10)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn('Queue: 4A', preview.stdout)
            self.assertFalse((root / 'state.json').exists())
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn('issued task 3D', result.stdout)


if __name__ == '__main__':
    unittest.main()
