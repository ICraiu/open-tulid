# Qwen Implementation

Implement exactly the assigned task, no larger adjacent ambition. The task was intentionally sized to fit within a local-model execution envelope; preserve that boundary.

Keep the change coherent, satisfy the listed acceptance criteria, and add or update tests appropriate to the task. If the task cannot be completed without expanding scope beyond its stated contract, stop and explain the blocking mismatch rather than silently broadening the work.

Do not write planning markdown files during implementation transitions unless the task explicitly requires documentation changes.

## Validation Failure Policy

Use validation failures as diagnosis, not permission to rewrite unrelated code.

Before changing code because of a failing validation command:

1. Identify whether the failure is inside the assigned task boundary.
2. If it is inside scope, make the smallest targeted fix and rerun the narrowest relevant command first.
3. If it is outside scope, pre-existing, environmental, flaky, or caused by a missing external service, do not chase it with broad edits. Report it in completion evidence as a scoped blocker.

Do not repeatedly run the full test suite while guessing. After one full validation failure, switch to the smallest failing test, module, or command that explains the problem. Return to the full required validation only after the targeted failure is understood and fixed.

Stop instead of thrashing when any of these are true:

- the same validation failure remains after two targeted fix attempts
- the fix requires editing files outside the allowed change surface
- the failure is unrelated to the task's changed files
- the failure points to dependency installation, host services, model availability, display/audio hardware, network, or other environment state

When stopping for one of those reasons, submit completion only if Tulid will accept the transition and include precise evidence. Otherwise exit non-zero with a concise blocker summary in the logs.
