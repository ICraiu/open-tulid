# Implementation Specification

Produce `implementation-spec.md` from the task, linked context, and current repository implementation when present.

Use the injected implementation-spec template as the structure and the injected diagram-requirements reference to decide which diagrams are required. Inspect repository files in the workspace when they exist; the specification must account for the code that is already written so the project can iterate instead of restarting from a blank design.

Prefer explicit contracts, state transitions, failure cases, ownership boundaries, data flow, and acceptance criteria over broad aspirations.

Carry every relevant technical-direction decision forward. Resolve the repository paths, public symbols, signatures, failure behavior, dependency seams, and deterministic validation commands needed by task breakdown and execution-contract authoring. Do not leave an implementation task to choose architecture or product behavior.

Before submission, state explicitly whether any implementation-blocking decision remains. If one remains, identify the single owner or human decision needed instead of hiding it in broad “open questions.”
