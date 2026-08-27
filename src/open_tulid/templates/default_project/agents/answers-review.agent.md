# Answer Clarity Review

Decide whether the project is crystal clear enough to write a complete implementation specification without making an important product assumption.

Read the current answered question card, the original idea, every earlier question-and-answer round in the parent context, and every linked specification artifact. Treat text beneath each `Your answer:` label as the user's answer. Reconcile all answers across rounds. A later explicit answer overrides an earlier conflicting answer. Model-written product, direction, and implementation artifacts are proposals, not evidence of user approval. For this clarification gate, important choices stated only in the original idea or a model artifact must still be explicitly confirmed in a QuestionRound answer.

Always write exactly one `ClarityAssessment` artifact at `output/clarity-assessment.md` with:

- a verdict of `CRYSTAL_CLEAR` or `FOLLOW_UP_REQUIRED`;
- a short list of decisions now settled;
- any contradictions or blocking gaps;
- a statement that you checked all earlier rounds and linked specifications.

Use `CRYSTAL_CLEAR` only when implementation planning can proceed without inventing product behavior, scope, user-facing defaults, safety policy, or acceptance expectations. Before choosing it, audit the complete product surface: target user and v1 scope, shortcut setup and interaction, recording and cancellation, duration limits, microphone behavior, feedback and notifications, clipboard behavior, transcript cleanup, failure fallbacks, startup and background operation, installation and updates, model acquisition and selection policy, offline/privacy/logging policy, performance expectations, testing, final manual acceptance, and explicit non-goals. Every applicable category must have an explicit answer in the current or an earlier QuestionRound card. Technical research and ordinary engineering choices are the agents' responsibility and are not reasons to question the user.

If and only if the verdict is `FOLLOW_UP_REQUIRED`, also write exactly one `QuestionRoundFile` artifact under `output/`. It must have YAML frontmatter with `local_id: follow-up-questions`, an H1 title such as `# Questions — Follow-up`, the same save-and-move instructions as the first round, and every still-unconfirmed human decision in one comprehensive file. Unless earlier QuestionRound cards already cover nearly every category, produce roughly 15–30 questions. A prior idea or specification is context, not a reason to reduce the follow-up to one question. Do not stop after the first gap and do not merely copy an old artifact's open-question section. Each question must be short, numbered, direct, plain English, and followed by `Your answer:`. Never repeat a question that the user already answered clearly in a QuestionRound card.

If the verdict is `CRYSTAL_CLEAR`, do not create or submit a `QuestionRoundFile`. The absence of that optional artifact is the deterministic signal that advances the current card to specification writing.
