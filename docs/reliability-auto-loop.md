# Reliability task loop

Run from a Linux shell using Python 3; no extra dependencies or cron required.

To replace the old saved progress and begin at task 3D:

```sh
python reliability_auto_loop.py --start-at 3D
```

Use `--start-at 3E` instead if 3D has already been completed. Resetting progress
requires OpenCode to have exited. For subsequent resumes, omit `--start-at`:

```sh
python reliability_auto_loop.py
```

The loop checks immediately, then every 900 seconds. While its worker is alive,
it logs the task, PID, and recent output. It also waits for other visible OpenCode
processes before restarting the shared model. Once the worker exits, it checks
the latest commit's subject and body for the issued task code as a whole token
(3D does not match 13D). A match advances to the next task; a mismatch logs the
failure and exits with status 1. This is the success heuristic, not independent
verification of the implementation. Git/model/launch errors also stop the loop.

Before each task it runs `ds4ctl stop`, then `ds4ctl start`, then
`opencode run --auto` with the task's plan, heading, shared instructions, and
commit requirement. The configured OpenCode default model is used unless `model`
is set to a provider/model identifier in the JSON configuration.

Edit `reliability_auto_config.json` to change `order`, `plan_files`,
`prompt_context`, model, or polling interval. Do not change task order while
resuming saved progress; use `--start-at` when changing the queue. Unknown tasks
are rejected before any worker starts. Preview without changing state or running
model commands:

```sh
python reliability_auto_loop.py --start-at 3D --dry-run
```

Progress is stored in `.reliability-auto-state.json`. The loop log is
`.reliability-auto.log`; full worker output is appended to
`.reliability-task-<code>.log`. Ctrl+C stops the loop and saves progress; an active
OpenCode worker continues. Restart without `--start-at` to monitor it again.
After a failure, inspect the logs and use `--start-at CODE` to explicitly retry
or select the next task. A lock prevents two loops from using the same state file.

Regression checks use temporary repositories and fake workers:

```sh
python -m unittest discover -s tests -p test_reliability_auto_loop.py -v
```
