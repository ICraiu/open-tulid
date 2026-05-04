# Domain CLI Integration Spec

## Purpose

Integrate the `open_tulid.domain` model with the existing CLI and vault path/project code.

The domain schema now knows how to represent, read, write, and structurally validate file-backed artifacts such as `IdeaTask`, `DefinedTask`, `TechnicalDirection`, and `CompletedTask`.

The CLI currently validates vault/project structure and Kanban task links with non-domain logic. That should change for artifacts: the CLI/vault layer should connect filesystem locations to domain operations, while the domain layer owns artifact reading, writing, and validation.

This spec defines the next implementation slice: make `tulid vault validate` use the domain model as the single business-logic mechanism for artifact files.

## Current Codebase Context

Existing CLI entrypoint:

```text
src/open_tulid/cli/main.py
```

Existing vault validation:

```text
src/open_tulid/vault/validator.py
src/open_tulid/vault/links.py
src/open_tulid/vault/project.py
src/open_tulid/models.py
```

Existing domain model:

```text
src/open_tulid/domain/
  __init__.py
  schema.py
  templates.py
  transitions.py
  validation.py
  readers.py
  writers.py
```

Existing tests:

```text
tests/test_vault_validate.py
tests/test_project_create.py
tests/domain/
```

Important existing behavior:

- `tulid vault validate` must still validate configured projects.
- Project directories must still contain `kanban/`, `docs/`, and `tasks/`.
- Kanban validation must still reject invalid Kanban task rows.
- Kanban task links must still resolve to files inside the same project's `tasks/` directory.
- Existing tests must remain meaningful. Update them only when the new domain validation intentionally changes the expected behavior.

Architectural rule:

- Domain code owns business logic: artifact reading, artifact writing, artifact validation, templates, transitions, and file-link validation.
- CLI/vault code owns system concerns: config loading, project discovery, filesystem path selection, command output, process exit codes, and converting domain reports into user-facing CLI reports.
- Do not create a second artifact parser, writer, or validator in the CLI/vault layer.

## Goal

After this slice, `tulid vault validate` should:

1. Preserve existing project directory validation.
2. Preserve existing Kanban board structure and task-link validation until Kanban is modeled in the domain.
3. Ask the domain layer to read known task files in `tasks/` as domain artifacts.
4. Ask the domain layer to read known docs files in `docs/` as domain artifacts when they match supported artifact conventions.
5. Ask the domain layer to validate domain file links using an in-memory artifact registry built from explicit project files.

## Single Mechanism Rule

For artifact files, there is one mechanism for each business operation:

- Reading artifact files: `open_tulid.domain.readers`
- Writing artifact files: `open_tulid.domain.writers`
- Validating artifact structure and links: `open_tulid.domain.validation`

The CLI/vault layer must not duplicate these mechanisms. Its role is to decide which filesystem paths are relevant, choose the requested domain state/template for those paths, call the domain API, and display the result.

## Non-Goals

Do not implement:

- New CLI commands unless this spec explicitly asks for them.
- LLM calls.
- Task execution.
- Automatic artifact repair.
- Vault-wide semantic quality checks.
- A database or persistent artifact index.
- Kanban state transitions.
- Full automatic inference for every possible Markdown file in the vault.

Do not add new business validation rules to the CLI/vault layer. If new artifact rules are needed, add them to the domain layer.

The existing Kanban validator may remain for this slice because Kanban is explicitly outside the current domain model. Treat it as a temporary non-domain subsystem, not a pattern to extend for artifacts.

## High-Level Design

Add a vault integration layer that invokes domain readers, writers, and validators.

Suggested new module:

```text
src/open_tulid/vault/domain_integration.py
```

Suggested public functions:

```python
validate_project_domain_artifacts(project: Project) -> ValidationReport
build_project_artifact_registry(project: Project) -> ArtifactRegistryBuildResult
read_project_artifact(path: Path, project: Project, registry: ArtifactRegistry | None = None) -> ArtifactReadResult
```

The names may differ if a cleaner local pattern emerges, but keep the behavior explicit and small.

This module must not contain independent artifact parsing, writing, or validation logic. It may:

- choose which domain state/template to request for a known path
- call `read_artifact_file()` or another domain-owned reader helper
- call `write_artifact_file()` if future CLI workflows need artifact output
- call `validate_artifact()`
- build and pass `ArtifactRegistry`
- translate domain results into CLI/vault reports

The existing `validate_project(project)` in `src/open_tulid/vault/validator.py` should call this vault-domain integration layer after directory and Kanban validation.

## Result Object Bridging

There are currently two validation result models:

```text
open_tulid.models.ValidationReport
open_tulid.domain.schema.ValidationReport
```

Do not create a third validation model.

For artifact validation, the domain report is authoritative. The CLI/vault report is only an aggregation and presentation object used by existing CLI output.

Add a small adapter that converts domain validation errors into CLI/vault validation errors:

```text
domain ValidationError:
  path: str | None
  location: str
  message: str

vault ValidationError:
  path: Path | None
  line: int | None
  message: str
```

Rules:

- Preserve the artifact path when available.
- Set `line=None` because domain errors currently do not carry source line numbers.
- Include the domain `location` in the message so users can find the failing section or field.

Example message:

```text
Domain artifact validation failed at section.Idea.field.Idea: Field 'Idea' must contain non-empty text
```

## Artifact Discovery

This slice should support a conservative, explicit discovery policy.

Do not scan every Markdown file in the vault recursively.

Validate only these project-local files:

```text
<project>/tasks/*.md
<project>/docs/*.md
```

Do not descend into subdirectories in this slice.

Rules:

- Files in `tasks/` should be read as task artifacts.
- Files in `docs/` should be read as docs artifacts.
- Non-Markdown files in `tasks/` and `docs/` should be ignored for now unless existing validation already rejects them.
- Missing directories are already handled by `validate_project`; do not duplicate those errors in the domain layer.

## Artifact State Selection

The domain reader requires the caller to choose:

```text
path
ArtifactState
Template
```

The reader must not infer state from file path. The CLI/vault integration layer is allowed to choose which state/template to request for known project locations because that is a system-location concern.

Use this initial state selection policy:

### Task Files

For each Markdown file in:

```text
<project>/tasks/*.md
```

attempt to read it as one of these task states, in this order:

```text
CompletedTask
DefinedTask
IdeaTask
```

Rules:

- Try each state with its built-in template.
- Use the first successful state in the priority order above.
- If none succeed, report a validation error explaining that the task file does not satisfy any supported task artifact state.
- Do not report ambiguity merely because a more-specific artifact also satisfies a less-specific template. For example, a valid `DefinedTask` may also satisfy `IdeaTask` because unknown sections are preserved. In that case, choose `DefinedTask`.
- Do not infer the state from filename.
- Do not mutate the file.

Rationale:

- `CompletedTask` has the most structure, then `DefinedTask`, then `IdeaTask`.
- Trying most-specific first avoids treating a completed task as merely an idea if extra sections are preserved.

### Docs Files

For each Markdown file in:

```text
<project>/docs/*.md
```

attempt to read it as one of these docs states:

```text
TechnicalDirection
TechnicalSpec
```

Rules:

- Try each state with its built-in template.
- Use the first successful state in the priority order above.
- If none succeed, do not report an error yet unless the file appears to be referenced by a domain file link.
- Do not report ambiguity merely because a file satisfies a looser template after preserving unknown sections.

Rationale:

- Existing projects may already contain arbitrary docs. This slice should avoid rejecting unrelated docs by default.
- Referenced docs should be validated because file links are structural domain data.

## Artifact Registry

Domain `FILE` and `FILE_LIST` validation uses an in-memory `ArtifactRegistry`.

Build one registry per project.

Registry keys must be path strings that match the values used in artifact fields.

Support these path forms:

1. Project-relative paths:

```text
tasks/Task 1.md
docs/Technical direction.md
```

2. Vault-relative paths including project name:

```text
Agent/tasks/Task 1.md
Agent/docs/Technical direction.md
```

3. Absolute filesystem paths, only for files inside the project.

Rules:

- Register each successfully read artifact under all supported path aliases.
- Do not register files outside the current project.
- Do not read files outside the explicit project `tasks/` and `docs/` files being validated.
- Use the domain artifact's canonical `path` as a project-relative path unless there is a strong reason not to.

Example:

For:

```text
<vault>/Agent/docs/Technical direction.md
```

register aliases:

```text
docs/Technical direction.md
Agent/docs/Technical direction.md
<absolute path>
```

If duplicate aliases point to different artifacts, report a validation error. Do not silently overwrite.

## Two-Pass Validation

Use a two-pass approach. If a new helper is needed to read candidate artifacts before registry-backed link validation runs, put that helper in the domain package, not in the CLI/vault package.

### Pass 1: Read Candidate Artifacts

For each candidate task/doc file:

- Try the supported states for that location.
- Store successful artifacts.
- Store read failures for later reporting only when required by the policy above.
- Do not run link validation yet unless a registry is already available.

Important:

The current domain reader validates links when a template includes link validators. For pass 1, it may be necessary to add a domain-owned helper that tolerates unresolved links until pass 2.

If the current domain reader rejects registry-backed links without a complete registry, choose one of these implementation approaches:

1. Add a small domain helper for parsing candidate artifacts without link validators, but keep it internal and tested.
2. Build a partial registry from artifacts that can be parsed without link validation, then revalidate with the complete registry.
3. Read docs artifacts first, build registry aliases, then parse task artifacts, then revalidate all referenced artifacts with the complete registry.

Prefer the simplest approach that preserves the domain rule: final validation of linked files must use the in-memory registry.

### Pass 2: Validate With Registry

After building the project registry:

- Revalidate each successfully read artifact with the registry.
- Convert domain validation errors into vault validation errors.
- Validate that referenced artifacts exist in the registry.
- Validate that referenced artifacts validate against their own template.

## Integration With Existing Kanban Validation

Existing Kanban validation checks only that linked task files exist.

Keep that behavior for now because Kanban is not yet part of the domain model. Do not extend this non-domain validator to inspect artifact contents.

After this integration:

- A Kanban task link to a missing file still fails as before.
- A Kanban task link to an existing but structurally invalid task file should also cause `vault validate` to fail.
- The error for structural invalidity should point to the task file, not only to the Kanban file.

Example:

```text
Agent/tasks/Task 1.md:
    Domain artifact validation failed at section.Idea.field.Idea: Field 'Idea' must contain non-empty text
```

## Minimal Supported File Examples

### IdeaTask

```markdown
## Idea

Idea: Add vault validation
```

### TechnicalDirection

```markdown
## Direction

Direction: Use the domain schema validator
```

### DefinedTask

```markdown
## Idea

Idea: Add vault validation

## Technical direction

Direction: docs/Technical direction.md
```

### CompletedTask

```markdown
## Idea

Idea: Add vault validation

## Status

Status: done

## Proof

Evidence: Tests pass
Changed files: tasks/Task 1.md
Validation result: pytest
```

## CLI Output

Keep the existing output structure from `src/open_tulid/cli/main.py`.

Current output includes:

```text
Checked N projects.
Checked N kanban files.
Checked N task links.
```

Do not add a new counter in this slice unless it is straightforward and all tests are updated.

If adding a domain artifact counter is easy, use:

```text
Checked N domain artifacts.
```

But this is optional for this slice. The important behavior is that domain errors appear in the existing error list and cause `vault validate` to exit with code `1`.

The CLI must not decide whether artifact content is valid. It should only render the domain result.

## Test Guidance

Tests are still needed for integration behavior, but do not rewrite the existing domain test suite.

Existing tests must continue to pass unless their expectations conflict with the integration behavior described here.

Required test updates:

- Existing `tests/domain/` tests should remain focused on domain behavior.
- Existing `tests/test_vault_validate.py` tests should continue to pass.
- Add integration tests to `tests/test_vault_validate.py` or a new `tests/test_vault_domain_validate.py`.

Recommended integration tests:

1. `vault validate` passes for a project with:
   - valid Kanban file
   - linked valid `IdeaTask` in `tasks/`

2. `vault validate` fails when a linked task file exists but is not a valid supported task artifact.

3. `vault validate` passes for a `DefinedTask` that links to a valid `TechnicalDirection` in `docs/`.

4. `vault validate` fails for a `DefinedTask` whose `Direction` field references a missing docs artifact.

5. `vault validate` fails for a `DefinedTask` whose linked `TechnicalDirection` exists but does not validate.

6. `vault validate` passes for a `CompletedTask` with `Status = done` and valid proof fields.

7. `vault validate` fails for a `CompletedTask` with `Status = done` and no `Proof` section.

8. Existing arbitrary docs in `docs/` are ignored when they are not referenced by any domain artifact.

9. Referenced docs are validated when linked from a domain artifact.

10. File link aliases work for both:

```text
docs/Technical direction.md
Agent/docs/Technical direction.md
```

11. Domain validation errors are shown by `tulid vault validate` and cause exit code `1`.

Do not add brittle assertions for the full Rich output. Assert:

- exit code
- important message substring
- relevant path substring when practical

## Implementation Steps

### Step 1: Add Vault-Domain Integration Adapter

Create:

```text
src/open_tulid/vault/domain_integration.py
```

Responsibilities:

- Convert `Path` values to domain path strings.
- Convert domain validation reports into vault validation reports.
- Build per-project artifact registry aliases.
- Invoke domain APIs to validate project task/docs artifacts.

Keep this module independent from Typer/Rich.

Do not implement Markdown parsing, field validation, link validation, or artifact serialization here. Those belong in `open_tulid.domain`.

### Step 2: Add Artifact Candidate Discovery

Implement small helpers:

```python
iter_task_artifact_files(project: Project) -> list[Path]
iter_doc_artifact_files(project: Project) -> list[Path]
```

Rules:

- Only direct `*.md` files.
- Sorted order for deterministic output.
- Return an empty list if the directory is missing.

### Step 3: Implement State Selection

Implement helpers that choose supported templates and call the domain reader:

```python
read_task_artifact_candidate(path: Path, project: Project, registry: ArtifactRegistry | None) -> ArtifactReadAttempt
read_doc_artifact_candidate(path: Path, project: Project, registry: ArtifactRegistry | None) -> ArtifactReadAttempt
```

Suggested internal result:

```text
ArtifactReadAttempt
  artifact: Artifact | None
  state: ArtifactState | None
  report: domain.ValidationReport
  matched_count: int
```

Rules:

- The first successful match in priority order means success.
- Zero matches means failure for task files, ignored for unreferenced docs.
- More than one match is allowed when the earlier match is more specific and later matches are looser templates.

### Step 4: Build Registry

For each successfully read artifact:

- Add aliases.
- Detect duplicate alias conflicts.
- Use `ArtifactRegistry.register()` for canonical path if helpful, but alias registration may require direct writes to `artifacts_by_path`.

Do not change `ArtifactRegistry` unless needed.

### Step 5: Final Validation

For all task artifacts and referenced docs artifacts:

- Re-run the domain validator, `validate_artifact(artifact, registry)`.
- Convert errors into vault errors.
- Include path and location in messages.

### Step 6: Wire Into `validate_project`

In:

```text
src/open_tulid/vault/validator.py
```

after existing Kanban validation, call:

```python
domain_report = validate_project_domain_artifacts(project)
```

Then merge:

```python
report.errors.extend(domain_report.errors)
```

If a new counter is added, merge it here as well. Otherwise do not change `open_tulid.models.ValidationReport`.

### Step 7: Adapt Tests

Run:

```text
pytest tests/domain
pytest tests/test_vault_validate.py
pytest
```

Fix only tests whose old fixture files are now invalid domain artifacts.

Important:

Current test helper `_make_task()` writes empty task files. Domain validation will make linked empty task files invalid.

Recommended adaptation:

- Change `_make_task()` in vault validation tests to write a minimal valid `IdeaTask` by default.
- For tests that only check missing task behavior, do not create the task file.
- Add a separate helper for intentionally invalid task files when needed.

Suggested default task content:

```markdown
## Idea

Idea: Task 1
```

## Edge Cases

### Empty Task File

An empty linked task file should fail domain validation.

### Arbitrary Unlinked Docs

An arbitrary Markdown file in `docs/` should not fail validation unless it is referenced by a domain artifact.

### Ambiguous Artifact

If a file can be read as more than one supported state, report ambiguity.

### Relative Path Separators

Use POSIX-style `/` in domain paths even on platforms where `Path` uses a different separator.

### Files Outside Project

Do not register or validate file links outside the project in this slice.

If a domain file references `../Other/file.md`, it should fail because the registry has no such path.

## Acceptance Criteria

This slice is complete when:

- `tulid vault validate` still validates project directories and Kanban files as before.
- Linked task files are structurally validated as domain task artifacts.
- `DefinedTask` links to `TechnicalDirection` validate through an in-memory registry.
- Missing or invalid linked domain artifacts cause `vault validate` to fail.
- Arbitrary unreferenced docs do not fail validation.
- Domain validation errors are converted into CLI validation errors with useful paths and locations.
- No domain validation reads outside the current project's direct `tasks/*.md` and `docs/*.md` candidates.
- No vault-wide recursive scan is introduced.
- Existing domain tests still pass.
- Existing CLI/vault tests are preserved or intentionally adapted for valid task file content.
- The full test suite passes.

## Review Checklist For The Implementer

Before calling this done, verify:

- Did you preserve existing `vault validate` behavior?
- Did you avoid recursive vault scanning?
- Did you avoid state inference inside the domain reader itself?
- Did the CLI integration layer explicitly choose requested artifact states based on project location?
- Did you avoid adding artifact parsing/writing/validation logic outside `open_tulid.domain`?
- Are file links checked against `ArtifactRegistry`, not the filesystem?
- Are missing linked docs reported as validation errors?
- Are invalid linked docs reported as validation errors?
- Are unreferenced arbitrary docs ignored?
- Are empty linked task files rejected?
- Does `pytest` pass?
