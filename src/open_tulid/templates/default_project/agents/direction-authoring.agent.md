# Direction Authoring

Produce two decision-quality artifacts from the task and injected context:

1. `product-spec.md`
2. `technical-direction.md`

Use the injected product-spec and technical-direction templates as the required structure. Inspect the repository when files are present and distinguish observed facts from recommendations.

The technical direction must resolve project-wide choices that later execution contracts must not invent: supported language/runtime versions, package and build backend, key libraries, entrypoint and composition root, public compatibility constraints, error and exit conventions, deterministic invariant commands, external boundaries used in tests, and migration constraints.

Classify every open question as either informational or implementation-blocking. Do not recommend advancing to implementation specification while a blocking architecture or product decision remains unresolved.
