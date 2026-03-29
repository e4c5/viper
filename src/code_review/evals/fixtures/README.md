# Eval Corpus

This directory holds the checked-in evaluation corpus for Phase 4 of the ADK adoption plan.

Files:

- `golden_pr_review_cases.json`: golden PR review scenarios with expected `FindingsBatchV1` output
- `reply_dismissal_eval_cases.json`: reply-dismissal scenarios with expected `ReplyDismissalVerdictV1` output

Why this exists:

- keeps a small, stable corpus in the repo
- lets us validate fixture shape in unit tests before wiring a fuller ADK eval harness
- gives us a concrete place to add regression cases when prompt/runtime behavior changes

Current scope:

- local checked-in corpus
- typed loader/validator in `code_review.evals.corpus`
- minimal local harness in `code_review.evals.local_runner`
- CLI entrypoint via `code-review-eval`
- no CI integration yet
- no agent/model-backed scoring harness yet

Run locally:

```bash
code-review-eval
code-review-eval --execution parser
code-review-eval --execution adk
code-review-eval --suite golden_pr_review
code-review-eval --suite reply_dismissal
```

Execution modes:

- `--execution parser`: validates the current parser seams against the checked-in expected outputs
- `--execution adk`: runs the actual review/reply-dismissal agent factories through the ADK runner
  path and scores the parsed outputs against the expected corpus

The next Phase 4 step is to improve scoring beyond exact-match checks and decide whether to wire
`code-review-eval --execution adk` into CI or a scheduled quality gate.
