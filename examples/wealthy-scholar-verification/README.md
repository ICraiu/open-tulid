# Wealthy Scholar global verification migration

This is a **staged project-owned setup change**, stored in Tulid because the
application source must remain untouched. It is not installed in the live
tracker or application. Do not treat the existing backend-only policy as
verification of the research product.

The read-only inventory on 2026-09-09 found:

| Component | Existing entry point | Required setup before implementation admission |
| --- | --- | --- |
| Backend | `backend/package.json`: `node --test`; 17 `tests/**/*.test.js` files | Retain the 17-file discovery floor and add dedicated contract/parity tests. Existing Mongo integration tests skip when Mongo is unavailable; provide deterministic fixtures or a declared test dependency. |
| Python/research | Absent | Python 3.12 package, committed `research/uv.lock`, pytest in the locked environment, contract tests and research pipeline tests. |
| Frontend | `frontend/package.json`: `vite build`; no tests or npm lock | Commit the npm lock and add tests under `frontend/tests/`. |
| Integrated behavior | `tests/package.json`: `node --test`; smoke/live tests need existing services | Add `tests/deterministic/` journeys using fake providers and owned data fixtures. Live/smoke tests remain a separate plan-6 gate. |

`tools/verify_project.py` is the application's global entry point. It uses the
existing Node runner, pytest/JUnit, and Vite build script; Tulid gains no component
schema, test platform, task command selection, or writable-file allowlist.
`NODE_SUITES`, `PYTHON_SUITES`, and `MINIMUM_FILES` are project-wide discovery
expectations. All listed components are required; **none are deferred** in this
research-product baseline. If product scope explicitly defers another component,
record the decision in the project baseline before migration, rather than making
its check conditional on a directory's existence.

The runner discovers every matching file, runs each file, rejects empty or skipped
tests, and requires actual frontend output in `.open-tulid/project-verification/`.
New files matching these conventions automatically join acceptance. To introduce
another component, extend these project expectations and the global preparation
commands in the setup change. Deleting required discovery or reducing the audited
backend floor fails. Tests must actually compare shared contract fixtures and
canonical hashes; a test name alone is not semantic proof. Review of test quality
and legitimate refactors remains with the existing review transition (plans 4E/6).

Before admitting dependent tasks, on an **isolated application/tracker copy**:

1. Establish the missing manifests, locks, meaningful test entry points, and
   deterministic provider/data fixtures. Copy `tools/verify_project.py` into that
   application copy. This repository intentionally supplies no placeholder tests
   to pretend the missing product work is implemented.
2. Extend the project's `Docker.tulid` from each configured worker image with
   Node/npm, Python 3.12, and uv. Pin tool versions in that project image recipe;
   put pytest and research dependencies in the committed uv lock. Provide a
   writable cache/home for the declared UID. The supplied `Docker.tulid` also
   includes MongoDB 7. `tools/with_test_database.py` starts a private loopback
   database in a temporary directory, overrides any inherited `MONGO_URI`, and
   removes it after verification. Persistence checks must execute rather than
   skip when no database is available. Verification receives no model/completion
   credentials and never connects to the live application database.
3. Run `python3 tools/verify_project.py --inventory` in the isolated copy; it
   must report every expected suite. Build the project image and run the ordered commands in this `contract.yaml`
   against a clean candidate in that image. `npm ci` and `uv sync --locked` must
   succeed without changing committed locks. Build output must remain under
   `.open-tulid/`; source under `dist`, `build`, or `output` is deliverable and is
   not silently exempted from mutation checks.
4. Require a green baseline with all components executed, and deliberate failing
   backend, parity, frontend, and integrated cases. Only then install this policy
   in the isolated tracker and freeze it for implementation/review admission.
   Install in the active project only as a separate migration after this proof.

The effective policy changes from one backend command to ordered lockfile
preparation for backend, frontend, integration, and research, followed by the
complete project entry point. The exact policy and order are frozen with each
job. Worker and verifier use the same locally resolved image ID. Historical jobs
without a frozen environment are blocked from host fallback; old reports keep
only their original guarantees.

Evidence is in the verifier's complete command logs and the entry point's
`.open-tulid/project-verification/report.json`, per-file TAP logs, Python XML,
and frontend build log. Routine acceptance must use deterministic fixtures;
backend tests do not establish browser usability or production provider behavior.

The isolated 2026-09-10 backend baseline executed 226 tests with zero skips in the
project image using its private MongoDB and disabled external networking. This
establishes the existing backend baseline only; it does not establish the missing
cross-language components or the complete product acceptance inventory.
