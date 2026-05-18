# Final Spec Review: Domain CLI Integration

## Verdict

**PASS with one minor cleanup note**

The implementation is now aligned with `docs/domain-cli-integration-spec.md` for the intended integration slice. The major architectural and behavioral requirements are implemented:

- artifact file reading goes through `open_tulid.domain.readers.read_artifact_file()`
- artifact validation goes through `open_tulid.domain.validation.validate_artifact()`
- vault/CLI code orchestrates project paths, state selection, registry construction, and output conversion
- final link validation is strict and registry-backed
- deferred link validation is explicit through `validate_links=False`
- direct `tasks/*.md` and `docs/*.md` candidates are used
- nested docs are not read as artifacts
- project-relative, vault-relative, and absolute aliases are supported for valid docs
- invalid project-relative and vault-relative referenced docs are reported without degrading to only `not found in registry`
- non-UTF-8 task files now preserve the domain reader error
- `matched_count` was removed
- domain path strings are POSIX-normalized

Verification:

```text
pytest
219 passed
```

## Review Changes Applied Correctly

### 1. Vault-Relative Invalid Docs

**Status:** Fixed

The implementation now builds a direct-doc candidate alias lookup and tracks failed doc aliases. This lets vault-relative links such as:

```text
Agent/docs/Technical direction.md
```

resolve back to the direct docs candidate when the doc exists but failed to parse.

Covered by:

```text
test_invalid_vault_relative_doc_fails_with_doc_error
```

### 2. Specific Domain Reader Errors

**Status:** Mostly fixed

The non-UTF-8 task test now requires:

```text
not valid UTF-8
```

instead of allowing the generic unsupported-artifact fallback.

Covered by:

```text
test_non_utf8_task_file_fails
```

### 3. `matched_count`

**Status:** Fixed

`matched_count` was removed from `ArtifactReadAttempt`.

### 4. POSIX Path Normalization

**Status:** Fixed

`_path_to_domain_string()` now normalizes relative paths through:

```python
Path(os.path.relpath(path, project.path)).as_posix()
```

Registry aliases also normalize canonical artifact paths before constructing vault-relative aliases.

## Remaining Minor Cleanup

### Reader Duplicate Section/Field Error Detection Is Too Narrow

**Severity:** Low

`_is_reader_error()` is intended to preserve reader-level errors during state selection, including duplicate section and duplicate field errors. The current logic checks for duplicate text in the error location:

```python
if error_location.startswith("section.") and "Duplicate section" in error_location or "Duplicate field" in error_location:
```

But duplicate messages are stored in the error message, not the location. As a result, duplicate section/field parse errors may still be collapsed into the generic unsupported-artifact message.

This does not break the core integration behavior and does not affect the currently tested happy paths. It is an error-quality cleanup.

Suggested fix:

- Pass the full `ValidationError` into `_is_reader_error()`, not only `location`.
- Check `err.message` for `"Duplicate section"` and `"Duplicate field"`.

Suggested test:

- Add a `vault validate` test for a task file with duplicate `## Idea` sections.
- Assert the CLI output includes `Duplicate section`, not only `supported task artifact`.

## Acceptance Criteria

| Criterion | Status |
|---|---|
| Existing `vault validate` behavior preserved | PASS |
| Domain reader used for artifact files | PASS |
| Domain validator used for artifact validation | PASS |
| Deferred link validation explicit | PASS |
| Direct task/doc candidate discovery | PASS |
| No recursive vault scanning | PASS |
| Registry aliases for valid artifacts | PASS |
| Missing linked docs fail | PASS |
| Invalid linked docs fail with useful domain errors | PASS |
| Arbitrary unreferenced docs ignored | PASS |
| Nested docs not read | PASS |
| Non-UTF-8 reader error preserved | PASS |
| POSIX path normalization | PASS |
| Full test suite passes | PASS |

## Conclusion

This slice is passable. The implementation satisfies the domain/CLI integration spec at the level needed to move forward. The only remaining item is a small diagnostic-quality improvement for preserving duplicate section/field reader errors during state selection.

