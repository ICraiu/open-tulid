# Implementation Spec Template

The following reference is part of your injected instructions. Use it directly; do not assume tracker filesystem access.

---

# Implementation Specification Template

## When to use

Use this spec after a technical direction or decision document has selected the approach.

Use it for:

- new product or platform capabilities
- substantial feature implementation
- migrations with implementation complexity
- API or contract changes
- agentic implementation workflows
- cross-service behavior changes
- correctness-sensitive work
- features with complex states, validation, permissions, or failure behavior

The goal of this document is to make the implementation unambiguous enough that engineers or agents can build the system correctly.

---

# 1. Purpose

- What is being built?
- What technical direction or decision does this implement?
- What user, product, or system outcome should this enable?
- Link to the related technical direction or decision document.

---

# 2. Scope

## In scope

- Capability 1
- Capability 2
- Capability 3

## Out of scope

- Explicit non-goal 1
- Explicit non-goal 2
- Explicit non-goal 3

## Assumptions and constraints

- Assumption 1
- Constraint 1

---

# 3. Current repository state

Summarize the relevant current implementation before proposing changes.

Include:

- existing packages and entrypoints
- existing files that already own adjacent behavior
- current interfaces that constrain the design
- known technical debt or mismatches that affect this work

---

# 4. Panalyzer structural evidence

This section is mandatory when repository files are present.

Include:

- repository root analyzed
- whether panalyzer was run directly
- relevant packages
- relevant files
- relevant symbols with signatures when knowable
- relevant reference edges or call paths
- unresolved or dynamic areas

Use tables where helpful.

---

# 5. Functional behavior

- Describe the behavior that must exist after implementation.
- Include user-visible behavior, system behavior, and failure behavior.

Include Mermaid diagrams for important runtime or state flows.

---

# 6. Module inventory

Define the implementation as a set of stable modules with explicit boundaries.

## Module inventory table

| Module | Type | Responsibility | Owns data/state? | Allowed dependencies | Forbidden dependencies | Used by |
|---|---|---|---|---|---|---|
|  |  |  |  |  |  |  |

For each substantial module, add a dedicated subsection:

### Module: `<name>`

- Purpose
- What belongs here
- What must not live here
- Owned types or data
- Public entrypoints
- Upstream callers
- Downstream dependencies
- Lifecycle responsibilities
- Failure ownership

---

# 7. Public interfaces and contracts

For every meaningful module with a public surface, specify exact interfaces.

For each interface, include:

- responsibility
- owner
- callers
- returns and side effects
- errors
- invariants

Add signatures when knowable:

```text
method_name(arg_name: Type, arg_name: Type) -> ReturnType
```

---

# 8. Planned change surface

This section is mandatory.

For each planned slice, specify:

| Slice | Module | Files to add | Files to edit | Primary symbols | Upstream callers | Downstream dependencies | Notes |
|---|---|---|---|---|---|---|---|
|  |  |  |  |  |  |  |  |

Also state:

- files or modules that should remain untouched
- where additive work is preferred over refactor
- where a refactor is required before feature work

---

# 9. Data model and persistence

- domain types
- config schema
- persisted artifacts
- cache or state files
- on-disk layout
- serialization contracts

---

# 10. Runtime flows

Describe startup, steady-state handling, retries, shutdown, and recovery.

Include Mermaid sequence or flow diagrams when the behavior is multi-step.

---

# 11. Failure handling

- expected failure modes
- normalization strategy
- error surfaces
- rollback or recovery behavior
- logging and observability needs

---

# 12. Testing and validation

List:

- exact commands
- targeted suites
- fixture or harness work
- manual verification when automation is insufficient

Tie validation back to modules or slices where possible.

---

# 13. SOLID review

Review the design against:

- Single Responsibility Principle
- Open/Closed Principle
- Liskov Substitution Principle
- Interface Segregation Principle
- Dependency Inversion Principle

Explain where the design satisfies or violates each principle, and justify deliberate exceptions.

---

# 14. Rollout or migration plan

- sequencing constraints
- compatibility concerns
- operator or user migration steps
- cleanup after rollout

---

# 15. Acceptance criteria

State observable, implementation-level completion conditions.

---

# 16. Open questions

List only the unresolved questions that materially affect implementation.
