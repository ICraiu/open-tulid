# Wealthy Scholar — Existing Product Acceptance Inventory

Status: planning evidence for plan 6, step 6D; no live worker chain or product acceptance
run is claimed. This inventory records the product acceptance scope that already exists in
the canonical specification and answers. It is evidence of current scope, not a source of new
features, and it governs the integrated product acceptance checks step 6D will exercise.

Parent: [reliability-06-product-completion-plan.md](../../docs/reliability-06-product-completion-plan.md).

## 1. Canonical sources used

Exact expectations below were confirmed against these authoritative files in the Wealthy
Scholar tracker and application repository, not against the older topic-clustering concept.

| Canonical source | Path | Role |
| --- | --- | --- |
| Product specification | `artifacts/2/ProductSpec/product-spec.md` | Owner-facing acceptance: user journeys, requirements R-*, gates, decisions. |
| Technical direction | `artifacts/2/TechnicalDirection/technical-direction.md` | Design decisions inherited by the spec. |
| Question rounds | `artifacts/{3,4,5,6}/QuestionRoundFile/follow-up-questions.md`, `tasks/3,4,5,6-questions-*.md` | Answers that settled MVP behaviour. |
| Implementation specification | `artifacts/6/ImplementationSpec/implementation-spec.md` | Canonical consolidated behaviour, module ownership, HTTP/Python contracts, acceptance checks. |
| Plan-3 breakdown | `tasks/7..20-*.md` and `artifacts/6/ImplementationTaskFile/*.md` | Implementing tasks and their intended verification behaviour. |
| Legacy acceptance file | `acceptance.yaml` | Superseded by the command-only global contract (see §0). |
| Project verification example | `examples/wealthy-scholar-verification/README.md`, `contract.yaml`, `tools/verify_project.py` | Ordered global checks and strict discovery this inventory relies on. |

## 0. Reconciliation with the current workflow

The legacy `acceptance.yaml` (`profile: project_tests`, single `npm test --prefix backend`
vertical slice) cannot establish product completion and is superseded. The live mechanism is
the command-only project global contract from `docs/implementation-contracts.md`: an ordered,
deterministic `contract.yaml` at the tracker root, executed in the resolved project image, with
no per-task command selection and no predicted writable-path allowlists. Accepted commands
alone are not coverage; discovery assertions and deliberate failure fixtures must prove each
suite executes real assertions. Legacy `ImplementationContract` artifacts and frozen jobs stay
readable for audit but are ignored for new runs.

The implementation specification's Section 12 focused checks are the task-text source of the
objective verification commands; the global policy carries them for scheduling/review. This
inventory therefore lists behavior and the task that owns it, and the verification seams that
prove it, without embedding executable command lists in task prose.

## 2. Agreed product journeys

Each journey is the observable user path promised by the canonical spec. `Spec §` refers to
the implementation specification sections; `PS §` to the product specification. Tasks are the
plan-3 breakdown IDs. "Verification" names the ordered project-wide checks and the
intentional-failure assertions that must detect broken behavior, not per-task command lists.

| ID | Journey (observable behavior) | Canonical source | Implementing task(s) | Verification that must detect breakage |
| --- | --- | --- | --- | --- |
| J1 | Freeze an immutable capability profile: user picks a versioned profile; evidence-linked atomic facets (confirmed and inferred) with preserved status, evidence IDs, level and confidence basis become a frozen snapshot. Inferred facets never render as observed facts. | Spec §4.1, §7; PS 6.1 | 9, 12, 20 | Snapshot/adapter and contract fixtures for confirmed/inferred facets; user-visible inferred labelling; invalid snapshot rejected. |
| J2 | Propose roots via independent routes (semantic seed, lexical/entity, local-scientific) with per-route agreement, representative works, rationale, and sanitized query previews across horizons `adjacent|balanced|distant`. | Spec §4.2; PS 6.1 | 12 | Deterministic `map_roots` stage, no live providers, horizon/route-disagreement/sanitized-preview fixtures. |
| J3 | Grouped approval: one atomic action approves the whole versioned root set, exclusions, and horizon; edits create a new proposal revision and remap; stale `proposalVersion` conflicts without partial mutation. Approval freezes the run manifest and the two-hour deadline. | Spec §4.3; PS 6.1 | 12, 17 | Atomic grouped-approval, revision-conflict, edit/remap, manifest/deadline-immutability fixtures; unit/UI grouped-approval tests. |
| J4 | Run progress and reload: user starts an authorized run and observes stage, progress, coverage, missingness, warnings and decision status over ordinary HTTP polling (no WebSocket), surviving page reload. | Spec §4, §8; PS 6.2 | 17, 20 | Polling/reload fixtures; run-status API; stage envelope fields. |
| J5 | Successful results: a completed run exposes 5–12 promoted title cards (default ≥5, at most 12), honest shortlisting (`no_thesis` for 0, `shortlist_incomplete` for 1–4, deterministic reduction >12), and each card links to its validated structured thesis. | Spec §4.10, §6; PS 6.3 | 16, 17, 20 | Shortlist-cardinality fixtures (0/1–4/5–12/>12); thesis title required; rejected candidates absent from user projection. |
| J6 | Valid no-result outcome: after adequate data and full gates, zero qualifying candidates is the successful `no_thesis` terminal, never shown as failure or "nothing found". | Spec §4.6, §8.1, §9; PS 6.2 | 13, 16, 17 | Adequate-data zero-candidate fixture ends `no_thesis`; distinct terminal UX in UI. |
| J7 | Evidence inspection: opening a thesis reveals atomic claims, exact evidence spans/source snapshots, epistemic labels, evidence roles/independence, strongest objection, unknowns, missing coverage, and the cheapest falsifying next test, without implying scholarly proof of demand. | Spec §4.10, §6, §7; PS 6.3 | 14, 16, 20 | Evidence-path navigation fixtures; paged claim/span resources; round-trip evidence cards to exact bytes. |
| J8 | Notifications and feedback: in-app notifications when root approval is needed and on completed, `no_thesis`, failed and timed-out runs; optional free-text feedback stored within bounds and never used as a ranking/training feature. | Spec §4.12, §6.4; PS 6.4 | 17, 20 | Notification append/read/read-state fixtures; feedback acceptance and non-use; UI notification polling. |
| J9 | Legacy behavior preservation: existing profile/conversation/question endpoints, response shapes, liveness independence, and the current conversation UI contract remain intact after the composition refactor; README drift corrected. | Spec §3, §5, §15 | 7, 20 | Full backend suite, preserved smoke tests, existing endpoint-shape fixtures, frontend build, unchanged legacy UI contract. |
| J10 | Preserve run/artifact/reproducibility boundary: each run is immutable and reproducible; corrected input creates a superseding run; large artifacts never enter user documents or workflow payloads. | Spec §7, §9, §14 | 9, 11, 17 | Idempotency-key semantics, immutable stage hashes, content-addressed artifact store, replay fixtures. |

## 3. Required failure cases

These are the product's specified terminal outcomes and fail-closed behaviors. Each must be
exercised with intentional breakage so the checks detect it.

| ID | Failure case (observable) | Canonical source | Implementing task(s) | Verification that must detect breakage |
| --- | --- | --- | --- | --- |
| F1 | Minimum-data failure: below the frozen `MinimumDataPolicy`, the run is `failed`/`minimum_data_not_met`, naming every threshold, numerator, denominator and observation; it can never produce `no_thesis`. | Spec §4.6, §9; PS 6.2 | 13, 17 | Below-minimum fixture must end `failed/minimum_data_not_met`, never `no_thesis`. |
| F2 | Provider/integrity failure: exhausted transient 429/5xx/network after bounded retries is `failed`; schema, licence, integrity, gate and timeout failures are not auto-retried. Undeclared provider substitution is an integrity failure. | Spec §9; PS 13 | 13, 17 | Bounded retry classification; undeclared-fallback integrity failure; `failed` terminal distinct from `timed_out`. |
| F3 | Timeout without partial results: after 7,200,000 ms (injected monotonic clock from authorization) the run is `timed_out`, stops new work, allows bounded cleanup, retains audit/intermediate artifacts, and exposes **no** partial thesis shortlist. | Spec §4, §8, §9 | 17 | Fixed-clock 7,200,000 ms fixture ends `timed_out`, emits failure notification, and the user thesis endpoint returns none. |
| F4 | Unsupported claims/fail-closed grounding: invalid spans/offsets, widened or normalized-only claims, retracted central sources, title-only support, injection text, malformed/injected model output and invalid judge references never promote. | Spec §4.5, §9; PS 7.4 | 14, 16 | Invalid-claim, retraction, injection, malformed-output fixtures are rejected; unsupported claims never reach a portfolio. |
| F5 | Inviability veto: explicit conflict with the user's time/cost constraints, technical realizability, or available evidence is a hard `inviable` rejection; unmeasured market viability remains a labelled hypothesis, never invoked as proof and never silently dropped. | Spec §4.9, §9; PS 13 | 9, 16 | Inviability fixtures gate correctly; market hypotheses remain labelled; hidden rejection retained. |
| F6 | Honest rejection ledger: rejected candidates and complete gate evidence persist append-only but stay hidden from normal users; admin may request them. | Spec §2, §4.10; PS 6.3 | 9, 16, 17 | Gate records persisted; user projection excludes rejections; admin view returns them with codes/evidence. |
| F7 | Reproducibility/integrity failure on replay: identical inputs produce equal hashes or declared tolerances and record `reused`; hash corruption, descriptor mismatch and corrupted artifact reads fail closed. | Spec §8, §9, §18 | 9, 11, 18 | Replay command matches goldens or tolerances; corrupted-read/collision fixtures fail closed; CLI exits per contract. |
| F8 | Phase Zero non-market evidence: the automated judge returns three explicit booleans, reasons and valid `object_refs`; `overall_pass` is true only when all three frozen dimensions pass; invalid output fails closed; result is labelled `research_thesis_validated`, never `market_validated`; the owner's continue/stop decision is recorded separately. | Spec §4.11, §6, §12 | 16, 18 | Judge-schema fixtures compute pass iff all dimensions pass; invalid references fail; label integrity fixture. |
| F9 | Data/privacy stay out of logs/errors: raw CVs, licensed spans, secrets and provider bodies never appear in structured logs, errors or details; details are allow-listed. | Spec §9, §10 | 11, 17, 20 | Redaction fixtures search logs/errors for raw private/licensed/secret/provider-body text. |
| F10 | Authentication/data-modelling terminal guards: a manual or card-only movement may not masquerade as verified implementation success; workflow decides success/failure/cancellation semantics, not state-ID or worker spelling. | Plan 6 decision 5; Spec §5 | runtime work | Domain/workflow tests with renamed states, custom task types, arbitrary worker assignment (plan 6 regression matrix). |

## 4. Existing usability expectations

Usability expectations already present in the spec (PS §10, §7.8; Spec §4, §6.4). They are
existing acceptance concerns, not new product requirements.

| ID | Usability expectation | Canonical source | Implementing task(s) | Verification that must detect breakage |
| --- | --- | --- | --- | --- |
| U1 | Keyboard navigation with visible focus and clear errors. | PS §10; Spec §6.4 | 20 | Browser/unit fixture performs the journeys via keyboard and asserts focus/error states. |
| U2 | Responsive layout and mobile-readable thesis cards; source links reliable. | PS §10; Spec §6.4 | 20 | Responsive/mobile fixture asserts readable cards and robust link state at small viewport. |
| U3 | Plain-language epistemic labels and decision status; inferred facets visibly distinct; no opaque "score". | PS §6.3, §10; Spec §2, §4 | 16, 20 | Label fixtures assert inferred/confirmed distinction and absence of top-level confidence number. |
| U4 | Honest shortlist and terminal UX: completed, `no_thesis`, `failed/minimum_data_not_met`, other failures, and `timed_out` are visually distinct; no partial timeout theses rendered. | Spec §4.10, §4.12; PS 6.2 | 20 | UI terminal-distinction fixtures; no partial timeout output. |
| U5 | Evidence navigation stays within bounds: paged claims/spans, exact source snapshots, role-complete portfolios and independent support or a preliminary label. | PS 6.3, 7.4 | 14, 16, 20 | Evidence-path/pagination fixtures; independence or preliminary-label rendering. |
| U6 | Notifications/feedback reachable and truthful: read state is tracked; feedback is bounded free text and never a ranking/training feature. | Spec §4.12, §17 | 17, 20 | Notification read-state and feedback-bound fixtures. |
| U7 | Internal-only surface preserved: the research UI stays behind the internal feature flag/allow-list with the existing no-auth warning; no API key or provider payload rendered to the client. | Spec §3, §10 | 7, 20 | Frontend fixtures assert no API key/private provider payload; allow-list/flag behaviour fixture. |

## 5. Mapping of requested behaviors to implementing tasks and expected evidence

Each requested behavior in the plan-6 step 6D scope maps to its implementing task and the
acceptance evidence that demonstrates it. This is the coverage map between the inventory above
and the plan-3 breakdown.

| Requested behavior (plan 6D) | Implementing task(s) | Expected evidence |
| --- | --- | --- |
| Grouped approval | 12, 20 | Atomic approve-all with version conflict/edit/remap fixtures; API reachable; UI grouped-approval/version-conflict tests. |
| Run progress/reload | 17, 20 | Polling/reload fixtures and run-status API error/terminal semantics; UI monitor without WebSocket. |
| Successful results | 16, 17, 20 | 5–12-card honest shortlist; thesis detail; terminal `completed`. |
| Valid no-result outcome | 13, 16, 17 | Adequate-data zero candidates end `no_thesis`; distinct UX. |
| Minimum-data failure | 13, 17 | Below-minimum fixture ends `failed/minimum_data_not_met`; every threshold reported. |
| Provider/integrity failure | 13, 17 | Bounded retry classification; undeclared fallback integrity failure; `failed` distinct. |
| Timeout without partial results | 17 | Fixed-clock 7,200,000 ms `timed_out`, no partial shortlist, failure notification. |
| Evidence navigation | 14, 16, 20 | Paged claim/span resources; evidence round-trip to bytes; evidence-path UI navigation. |
| Notifications/feedback | 17, 20 | Notification events/read state; feedback non-use; UI notification polling. |
| Legacy behavior preservation | 7, 20 | Backend suite preserved; existing endpoint shapes; existing conversation UI contract; README corrected. |
| Keyboard use | 20 | Keyboard navigation fixture asserts focus/error states. |
| Responsive layout | 20 | Mobile/readable-card fixture at small viewport; robust source links. |

## 6. Whole-product proof definition

Individual green tasks are necessary but not sufficient. A finished product additionally
passes the integrated journeys above in a clean environment: the delivered repository must
independently pass the ordered global policy and match the accepted source identity, and the
integrated product acceptance inventory must pass before the product is called finished.
Routine acceptance must use deterministic provider/data fixtures; backend tests alone do not
establish browser usability or production provider behavior (plan 6 gates).

Legacy semantics are preserved for audit: older `ImplementationContract` artifacts, `acceptance.yaml`,
and frozen jobs keep their original meaning and must not be upgraded retroactively. The current
`contract.yaml` and `verify_project.py` discovery runner are the verification surface this
inventory consumes; the missing application setup (frontend/backend/research/integration
manifests) must be resolved on an isolated copy before admitting dependent tasks, and the
migrated policy installed only after a green baseline with deliberate failing fixtures.
