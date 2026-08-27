# Comprehensive Question Authoring

Create one comprehensive question card that resolves every product decision a human must make before an implementation specification and implementation tasks can be written.

Read the original idea and all linked product, direction, and prior specification material. The material informs the questionnaire, but this workflow requires the important decisions to be explicitly confirmed in a Questions card. Product, technical-direction, and implementation-spec artifacts written by models are proposals and do not prove that the user approved the decisions inside them. Convert important choices stated only in the original idea or model artifacts into short confirmation questions.

Ask every still-unanswered question whose answer materially affects the product, user experience, scope, defaults, failure behavior, privacy, operation, acceptance behavior, or irreversible constraints. Do not stop after finding the first blocker. Audit the complete product surface: shortcut setup and interaction, recording and cancellation, duration limits, microphone behavior, feedback and notifications, clipboard behavior, transcript cleanup, failure fallbacks, startup and background operation, installation and updates, model acquisition and selection, offline/privacy/logging policy, performance expectations, testing, and final manual acceptance. Do not ask the user to research libraries, models, protocols, repository structure, or other technical facts that the project agents can determine themselves.

Write exactly one `QuestionRoundFile` artifact under `output/`. It must use this storage shape:

```markdown
---
local_id: initial-questions
---
# Questions — Round 1

Answer directly beneath every question. Save the file, then move this card from **Questions** to **Answers ready**. Moving the card is the submission signal; no special completion phrase is needed.

## 1. A short, direct question in plain English?

Your answer:

## 2. Another short, direct question in plain English?

Your answer:
```

Make the first round a complete decision questionnaire, not a copy of the first open-question section found in an old artifact. Unless earlier QuestionRound cards already contain explicit answers, produce roughly 15–30 questions and cover every applicable product category listed above. A prior idea or specification is context, not a reason to reduce the card to one question. Use one decision per numbered question. Keep every question short, concrete, neutral, and readable by a non-technical user. Include a brief example only when the choice would otherwise be ambiguous. Do not hide multiple questions in one paragraph. Do not propose answers on the user's behalf. Do not create a specification or implementation tasks in this transition.
