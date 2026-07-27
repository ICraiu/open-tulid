# Qwen Implementation Procedure

Implement exactly the assigned task, no larger adjacent ambition. The task was intentionally sized to fit within a local-model execution envelope; preserve that boundary.

Keep the change coherent, satisfy the listed acceptance criteria, and add or update tests appropriate to the task. If the task cannot be completed without expanding scope beyond its stated contract, stop and explain the blocking mismatch rather than silently broadening the work.

Use this sequence:

1. Read the current task. When an execution contract is present, treat its objective, change surface, interfaces, and checks as authoritative.
2. Inspect the relevant files and any named integration seams before editing.
3. Make the smallest coherent implementation of the assigned behavior.
4. Run the narrowest relevant check first.
5. Run every required project-level validation.
6. Compare the final changed paths with the allowed change surface when present, otherwise with the current task boundary.
7. Report a precise blocker if the work requires an architectural or product decision absent from the task.

Do not create planning reports or unrequested documentation. Edit or create a Markdown file only when it is inside the allowed change surface and the assigned behavior requires it.
