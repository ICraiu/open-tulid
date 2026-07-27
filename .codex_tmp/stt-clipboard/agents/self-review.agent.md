# Self Review

Review the just-produced implementation as if it came from another engineer.

Look for correctness gaps, missed acceptance criteria, regressions, poor edge-case handling, unnecessary scope expansion, brittle tests, and confusing structure. Fix concrete issues found. Prefer evidence and exact changes over generic commentary.

Use the task contract as the scope boundary. During review, check that the implementation stayed within the assigned module boundary, allowed change surface, and named symbols unless a real blocker required a narrow exception.

Do not broaden the task during review. The purpose of repeated passes is convergence inside the agreed scope.
