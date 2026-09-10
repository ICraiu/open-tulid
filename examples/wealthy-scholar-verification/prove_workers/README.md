# Plan 6F — prove actual configured workers and the complete product

Operator-run protocol for the real configured-worker experiment. The commit for
step 6F establishes the isolation and run-ledger mechanism (`prove_workers.py`),
records the actual configured worker/service identity (`run-ledger-6F.*`), and
keeps the ledger available for the sustained real runs that follow.

`prove_workers.py` is a tool, not acceptance criteria. It predicts no writable
paths; it never defines per-task commands; and it writes only into the workspace
it is given. `docs/implementation-contracts.md` still owns project-wide
verification.

## What is being proven

The plan (docs/reliability-06-product-completion-plan.md §6F) asks for proof of
the user's actual chosen workers and model services on an **isolated** checkout
and tracker copy, running at least three dependent implementation tasks through
planning → implementation → review → delivery, across language boundaries, with a
sustained run beyond one hour and a controlled stop/restart, repeated three
times, while recording every attempt, retry, failure, verification/promotion
evidence, duration, and manual intervention. Then the full remaining Wealthy
Scholar plan and product acceptance inventory run from a clean checkout.

This repository supplies the harness; the actual multi-hour real runs are the
operator continuation, because they compete for the single-slot model service
(`http://127.0.0.1:8080`, DeepSeek V4 Flash) and would not produce a bounded,
verifiable commit while another worker holds the slot.

## Isolation guarantees

- `copy_repo` creates a working checkout with independent Git objects and removes
  the origin remote, so ordinary experiment commits cannot push to the live project.
- `copy_tracker` copies the tracker into a new destination; existing experiments
  are never overwritten.
- `shield` refuses any configured path that resolves inside a live root
  (project repo or tracker), including symlinked paths. The tool exits before
  touching anything if a violation is found. This is the live-write guard 6F
  requires.
- `sanitize` redacts keys/tokens/secrets before they reach the ledger.
- A failed run is never hidden: `run_chain` records each attempt, each repair
  retry, and each delivery block, and the summary counts failures separately.

## How to run it

Self-check (deterministic, no model slot; used by CI):

```sh
.venv/bin/python \
  examples/wealthy-scholar-verification/prove_workers/prove_workers.py \
  --self-check
```

Prepare isolation and identity, then drive the real chain:

```sh
.venv/bin/python \
  examples/wealthy-scholar-verification/prove_workers/prove_workers.py \
  --source-repo /home/rawsteel/repo/wealthy-scholar \
  --source-tracker /home/rawsteel/repo/obsidian \
  --worker local_llm \
  --cases 3 --repeat 3 --sustained-hours 1.5 \
  --ledger-dir prove_workers_ledger
```

Set `TULID_CONFIG` to the isolated configuration file to keep jobs, leases, logs,
and daemon state under its parent directory without changing the process home.
Keep the ledger, configuration, checkout, and logs on persistent storage, such as
the Git-ignored `.reliability-proof/` directory. A temporary filesystem can lose
the entire experiment on a host restart. Historical tracker events are retained
in a sibling `tracker-history/` archive so their live recovery paths cannot run
inside the experiment.

Then run the real chain with the configured worker assignments against
the isolated tracker copy via the existing CLI, and call the tool again with a
worker runner that appends each attempt's evidence to the ledger. Append every
attempt, retry, failure, verification/promotion record, duration, and manual
intervention; never reset the ledger to hide a failed run.

## Identity recorded in this commit (2026-09-09)

| Item | Value |
| --- | --- |
| Worker | `local_llm` → `open-tulid/agent-opencode:latest` |
| Worker image id | `sha256:a9c97535d00789c7520b8c704e301671915b67c2a491e6d22e29c27eeab9a792` |
| Project image id | `sha256:291943d3a053c79da99239cc59f7a03f030823296de22d977f78d528d858fd3a` |
| Model service | `http://127.0.0.1:8080/health` → `{"status":"ok"}` (DeepSeek V4 Flash) |
| Host | `behemoth` |
| Source HEAD | `b41649e` |
| Wealthy Scholar HEAD | `3e695b4` (`7: Extract the backend composition root…`) |

These values were captured by the harness and stored in
`run-ledger-6F.{json,md}`.

## Honest status and evidence limits

This commit does **not** claim the sustained real-worker chain or the full
Wealthy Scholar product pass — none has been run. It provides: the isolation
mechanism, the live-write shield, sanitization, the faithful run ledger, and the
self-check plus regression tests that prove the mechanism works. The sustained
chain and product acceptance are the next operator step, and the ledger will
record their real outcomes.
