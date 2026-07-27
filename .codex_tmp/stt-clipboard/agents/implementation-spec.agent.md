# Implementation Specification

Produce `implementation-spec.md` from the task and injected linked context.

Use the injected implementation-spec template as the structure and the injected diagram-requirements reference to decide which diagrams are required. The result must be concrete enough that a later task-breakdown pass can derive many independently implementable tasks without rediscovering architecture, product intent, or hidden interfaces.

Prefer explicit contracts, state transitions, failure cases, ownership boundaries, data flow, and acceptance criteria over broad aspirations. Do not assume direct access to the tracker filesystem; all relevant source material must come from the task body, linked context injected into the prompt, repository files present in the workspace, and these instructions.

The goal is not a broad architecture memo. The goal is a build-ready execution specification that minimizes implementation-time decision making by later local-model workers.

## Panalyzer-first analysis rule

If a repository source tree is present in the workspace and `panalyzer` is available, run a full structural scan before finalizing the spec.

Preferred command:

```bash
panalyzer -a <repo-root>
```

Use that output as the primary source of truth for:

- package and file identities
- important method and function identities
- signatures
- internal reference edges
- candidate integration seams

If `panalyzer` is unavailable, perform an equivalent manual repository scan and state that panalyzer evidence was unavailable.

Do not invent module boundaries that contradict the structural evidence unless you explicitly justify the change as a proposed refactor.

## Hard output requirements

The final `implementation-spec.md` must satisfy all of the following. These are mandatory, not suggestions.

- It must include Mermaid diagrams wherever the template or diagram requirements call for a diagram.
- It must include a complete module inventory for the application, not just high-level subsystems.
- It must define the interface surface of every meaningful module.
- It must check whether each interface and dependency boundary is coherent and defensible.
- It must explicitly evaluate the design against SOLID principles and call out any justified exceptions.
- It must include a panalyzer-backed structural evidence section that lists the files, symbols, and references most relevant to the planned work.
- It must identify a proposed change surface: files likely to change, files likely to be added, and symbols likely to be introduced or edited.

If any of these are missing, the specification is incomplete.

## Structural evidence requirement

Include a dedicated section named `Panalyzer structural evidence` with at least:

- repository root analyzed
- whether panalyzer was used directly
- relevant packages
- relevant files
- relevant functions and methods with signatures when knowable
- relevant incoming or outgoing call edges
- unresolved or dynamic areas that panalyzer could not prove

This section is not optional for a repository-backed task.

## Module-first authoring rule

Start from module boundaries and interfaces, not from end-to-end flows.

Before writing runtime flows or implementation phases, define:

- the modules, packages, binaries, services, adapters, libraries, helpers, and entrypoints involved
- the responsibility boundary of each module
- which module owns each behavior, state transition, validation rule, and integration
- the allowed dependency directions between modules
- the public interfaces exposed by each module, including method or function signatures when they can be inferred

If the source material is not explicit enough, choose a concrete module split and document it. Do not leave downstream workers to invent boundaries.

## Change-surface rule

The implementation specification must contain a section named `Planned change surface`.

For each meaningful planned change, state:

- module or package
- concrete files to add or edit
- primary symbols to add or edit
- upstream callers
- downstream dependencies
- whether the change is additive, behavioral, or refactor-only
- files or boundaries that must remain untouched unless a later task explicitly widens scope

The later breakdown step will turn this section into task contracts. Make it precise.

## Specification standard

Write the specification so that a later breakdown pass can produce small, concrete, ticket-like tasks. That means the spec must make the following explicit wherever relevant:

- modules, packages, binaries, services, and entrypoints to create or change
- stable module boundaries and forbidden cross-boundary access patterns
- data models, persistent state, config keys, env vars, and on-disk file locations
- external interfaces and internal contracts, including function boundaries, CLI flags, IPC shapes, HTTP endpoints, event payloads, and error semantics
- internal module interfaces, including public methods or functions, signatures, input or output types, and side effects
- lifecycle and state transitions, including startup, steady-state behavior, retries, shutdown, and recovery
- validation strategy, including which commands, checks, or automated tests prove each area is correct
- sequencing constraints, including what must exist first to unblock downstream work

If the source material leaves an interface or behavior ambiguous, do not smooth over it. Either resolve it concretely from context or explicitly choose one option and state the choice.

## Output quality bar

The final specification should let a weaker implementation model execute without making high-impact design choices on its own. If a later worker would otherwise have to guess about interfaces, behavior, test scope, touched files, or integration boundaries, the specification is still too vague and must be made more concrete.
