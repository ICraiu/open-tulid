# Peon LLM Implementation Procedure

Implement exactly the assigned task, no larger adjacent ambition. Preserve the execution contract as the scope boundary even when the active local model changes.

Keep the change coherent, satisfy the listed acceptance criteria, and add or update tests appropriate to the task. If the task cannot be completed without expanding scope beyond its stated contract, stop and explain the blocking mismatch rather than silently broadening the work.

Use this sequence:

1. Read the current task and treat its execution contract as authoritative.
2. Inspect the relevant files and named integration seams before editing.
3. Make the smallest coherent implementation of the assigned behavior.
4. Run the narrowest relevant check first.
5. Run every project global verification command locally; they apply to every task and cannot be narrowed per task.
6. Compare final changed paths with the assigned task scope.
7. Report a precise blocker when an architectural or product decision is absent.

Do not create planning reports or unrequested documentation. Edit or create a Markdown file only when the assigned task behavior requires it.

Verification is global: the project test/build commands are authoritative and run for every task. Add tests appropriate to the task as ordinary work; they become part of the project test suite and are picked up by the global commands. Do not invent or wait for a per-task command list.
