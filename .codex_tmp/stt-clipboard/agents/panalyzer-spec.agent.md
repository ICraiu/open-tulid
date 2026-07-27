# Panalyzer-backed Specification Authoring

Use panalyzer as a planning aid when repository code is present in the workspace.

## Required behavior

If `panalyzer` is available:

1. run a full scan with method and reference data
2. identify the packages, files, and symbols relevant to the requested change
3. use that evidence to shape module boundaries and interface definitions
4. surface the likely change surface in the specification

Preferred command:

```bash
panalyzer -a <repo-root>
```

If the repository is large, you may inspect the full scan once and then focus only on the relevant files and symbols. Do not ignore the scan and revert to vague prose.

## Structural evidence expectations

The implementation specification should capture:

- exact file paths
- exact symbol names
- signatures when knowable
- call relationships that explain why a boundary matters
- places where the current architecture is already coherent
- places where the planned work needs a refactor before feature changes

## Refactor discipline

Panalyzer evidence is descriptive, not prescriptive.

If the existing structure is poor, propose a better module split. But do so explicitly:

- identify the current file and symbol locations
- identify the target boundary
- explain why the move or refactor is justified

Do not silently plan broad refactors without anchoring them to current files and symbols.
