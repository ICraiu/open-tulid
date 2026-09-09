# Run ledger — prove configured workers

- experiment: 20260909T170637Z
- started: 2026-09-09T17:06:37Z
- isolation repo: <isolated-repo-checkout>
- isolation tracker: <isolated-tracker-copy>
- shield violations: 0
- worker: local_llm
- hostname: behemoth
- source_head: {'sha': 'b41649e90711e66dc6e0aaa6b10e09c74b820fff', 'subject': 'implement plan 6, step 6E: deterministic fault and scripted chains'}
- project_head: {'sha': '3e695b4e576ca7440b0a91c307788144c548cf87', 'subject': '7: Extract the backend composition root without breaking existing APIs'}
- worker_images: {'image': 'open-tulid/agent-opencode:latest', 'id': 'sha256:a9c97535d00789c7520b8c704e301671915b67c2a491e6d22e29c27eeab9a792', 'created': '2026-08-16T18:22:31.088354042+02:00'}
- project_images: {'image': 'open-tulid/project-wealthy-scholar-codex:latest', 'id': 'sha256:291943d3a053c79da99239cc59f7a03f030823296de22d977f78d528d858fd3a', 'created': '2026-08-16T18:22:02.294057198+02:00'}
- model_service: {'health_url': 'http://127.0.0.1:8080/health', 'reachable': True, 'body_prefix': '{"status":"ok"}'}
- config: <sanitized machine config: worker_images.local_llm=open-tulid/agent-opencode:latest; model_proxy.peon.base_url=http://127.0.0.1:8080/v1>

## Chain

## Summary
- completed: 0
- failed: 0
- retries: 0
- duration_seconds: 0
- manual_interventions: 0
- note: Committed step 6F establishes this isolation + run-ledger harness and the recorded identity. The sustained real-worker chain (plan->implement->review->delivery, >1 hour, controlled stop/restart, 3x repeat) and the full Wealthy Scholar product acceptance run are operator continuations after this commit; this ledger will be appended with those attempts/failures/evidence and kept available.
