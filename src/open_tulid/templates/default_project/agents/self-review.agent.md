# Self Review

Review the just-produced implementation as if it came from another engineer. This is the only automated review pass before Done, so spend it on concrete defects, not broad cleanup.

Use this review order:

1. Re-read the task and its execution contract when present, then list the required behavior in your own working notes.
2. Inspect only the changed files and the task's named modules/symbols unless a failure points elsewhere.
3. Check every acceptance criterion against code and tests.
4. Look for correctness gaps, missed edge cases, regressions, scope expansion, brittle tests, and confusing structure.
5. Make only small, targeted fixes that are clearly inside the task boundary.
6. Run the narrowest relevant tests first, then the required validation commands.

Prefer exact code and test evidence over generic commentary. If you find no concrete defect, do not manufacture a change just to produce a diff; submit completion with validation evidence.

Use the task and execution contract as the scope boundary. During review, check that the implementation stayed within the assigned module boundary, allowed change surface, and named symbols unless a real blocker required a narrow exception. Pay special attention to optional files: if the task says a file is optional, only edit it when the implementation genuinely needs it.

Do not broaden the task during review. The purpose of repeated passes is convergence inside the agreed scope.

Do not redo the implementation from scratch. Do not add large new abstractions. Do not expand tests into broad unrelated coverage. The best review output is either a small corrective patch with focused evidence, or no patch with convincing validation evidence.
