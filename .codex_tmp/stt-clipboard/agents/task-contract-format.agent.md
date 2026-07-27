# Task Contract Format

Derived implementation tasks are contracts for a smaller implementation model.

Every task must reduce ambiguity about:

- where code may change
- which symbols are in scope
- what behavior must become true
- what behavior is explicitly out of scope
- how correctness is proven

## Required contract fields

Every task body must define all of the following in concrete repository terms:

- primary module
- concrete files to add
- concrete files allowed to edit
- primary symbols to add, edit, or preserve
- upstream callers
- downstream dependencies
- forbidden files, modules, or adjacent concerns
- acceptance criteria
- validation commands

## Signature discipline

When a task owns or changes a public interface, include the signature directly in the task body when knowable. If the exact signature is not yet certain, provide typed pseudocode that still constrains inputs, outputs, and side effects.

## Scope discipline

A good task should let Qwen implement one narrow boundary without inventing architecture. If a task still requires the implementer to decide:

- which module should own the behavior
- which files to change
- which public interface to introduce
- which call path to attach to
- how broad the refactor should be

then the task is not ready and must be decomposed further.
