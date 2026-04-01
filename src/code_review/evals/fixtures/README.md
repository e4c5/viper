# Eval Corpus

This directory holds the checked-in evaluation corpus for Phase 4 of the ADK adoption plan.

For the full developer workflow, see `docs/VALUATION-HARNESS.md`.

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
- checked-in parser baseline in `parser_eval_baseline.json`
- deterministic parser eval gate in GitHub Actions CI via `python -m code_review.evals.cli --execution parser`
- optional ADK-backed local execution via `code-review-eval --execution adk`

Run locally:

```bash
code-review-eval
code-review-eval --execution parser
code-review-eval --execution adk
code-review-eval --suite golden_pr_review
code-review-eval --suite reply_dismissal
python -m code_review.evals.cli --execution parser
```

Execution modes:

- `--execution parser`: validates the current parser seams against the checked-in expected outputs
- `--execution adk`: runs the actual review/reply-dismissal agent factories through the ADK runner
  path and scores the parsed outputs against the expected corpus

The next Phase 4 step is to improve scoring beyond exact-match checks.

`python -m code_review.evals.cli --execution parser` runs in CI as the deterministic eval gate.
Use `parser_eval_baseline.json` as the checked-in score snapshot to update intentionally when the eval corpus changes.
`code-review-eval --execution adk` remains the local/manual path for model-backed spot checks and for recording future ADK baselines.
