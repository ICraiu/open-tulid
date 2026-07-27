# Qwen Implementation

Implement exactly the assigned task, no larger adjacent ambition. The task was intentionally sized to fit within a local-model execution envelope; preserve that boundary.

Keep the change coherent, satisfy the listed acceptance criteria, and add or update tests appropriate to the task. If the task cannot be completed without expanding scope beyond its stated contract, stop and explain the blocking mismatch rather than silently broadening the work.

Treat the task contract as authoritative, especially:

- module boundary
- allowed change surface
- primary symbols and contracts
- non-goals
- validation requirements

Do not edit files outside the allowed change surface unless the task body explicitly permits it or you have a concrete blocker that makes the contract impossible.

Do not invent new public interfaces, broaden signatures, or re-architect neighboring modules unless the task body explicitly assigns that work.

When a task names exact symbols, preserve that symbol focus. Prefer a smaller complete implementation over a larger partial cleanup.

ATTENTION MODEL:
DO NOT WRITE MD FILES.
ONLY WRITE IMPLEMENTATION AS DESCRIBED IN THE GIVEN TASK
