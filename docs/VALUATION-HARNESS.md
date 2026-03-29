# Valuation Harness

This guide explains how Phase 4 evaluation works, when to run it, how to interpret failures, and when to update the checked-in baseline.

## What This Covers

Phase 4 adds a small checked-in evaluation corpus so we can detect regressions in review behavior when prompts, parsers, agent wiring, or runtime logic change.

Today the repo has two evaluation paths:

- `parser`: deterministic, local-data-only validation against checked-in expected outputs
- `adk`: model-backed local spot checks using the ADK runner

The parser path is the normal regression gate. The ADK path is a manual validation tool.

## Files

- Eval fixtures: `src/code_review/evals/fixtures/golden_pr_review_cases.json`
- Reply-dismissal fixtures: `src/code_review/evals/fixtures/reply_dismissal_eval_cases.json`
- Harness: `src/code_review/evals/local_runner.py`
- Eval CLI: `src/code_review/evals/cli.py`
- Baseline snapshot: `src/code_review/evals/fixtures/parser_eval_baseline.json`
- Baseline regression test: `tests/evals/test_parser_eval_baseline.py`

## How To Run

Run the deterministic parser eval:

```bash
python -m code_review.evals.cli --execution parser
```

Run the installed script form:

```bash
code-review-eval --execution parser
```

Run the ADK-backed local spot checks:

```bash
code-review-eval --execution adk
```

Run a single suite:

```bash
code-review-eval --suite golden_pr_review
code-review-eval --suite reply_dismissal
```

## How CI Uses It

GitHub Actions CI runs:

```bash
python -m code_review.evals.cli --execution parser
```

This is intentionally the parser-backed path because it is:

- deterministic
- fast
- independent of external model/API availability
- suitable as a normal pull-request gate

The ADK-backed eval path is not part of normal CI because it depends on model/runtime configuration and is better suited to manual validation.

## How To Interpret Failures

If parser eval fails, treat it as a regression signal. Usually one of these changed:

- fixture contents
- schema or parser behavior
- expected output shape
- local harness behavior

Start by running:

```bash
pytest -q tests/evals tests/test_cli_eval.py
python -m code_review.evals.cli --execution parser
```

Then compare:

- the failing case in the checked-in fixture
- the current output
- the checked-in baseline in `src/code_review/evals/fixtures/parser_eval_baseline.json`

## When To Update The Baseline

Update `parser_eval_baseline.json` only when the eval result changed intentionally.

Good reasons to update it:

- you added a new eval case
- you intentionally changed the expected output for an existing case
- you changed parser behavior on purpose and the new result is the desired one

Do not update the baseline just to silence an unexpected failure. First decide whether the change is:

- an intended product behavior change
- a fixture correction
- an unintended regression

## Baseline Update Workflow

1. Make the intended parser/eval change.
2. Run `python -m code_review.evals.cli --execution parser`.
3. Inspect which suite or case changed.
4. If the new result is intended, update `src/code_review/evals/fixtures/parser_eval_baseline.json`.
5. Run `pytest -q tests/evals tests/test_cli_eval.py`.
6. Re-run `python -m code_review.evals.cli --execution parser`.
7. Include a short explanation in the PR describing why the baseline changed.

## When To Use ADK Eval

Use `code-review-eval --execution adk` when you want a model-backed spot check after:

- prompt edits
- agent factory changes
- ADK runtime changes
- model/provider changes

This is useful for sanity-checking behavior, but it is not the main CI gate.

## Current Limitations

The current Phase 4 setup is intentionally compact:

- small corpus
- exact-match style scoring
- parser gate in CI
- ADK path for manual checks

Future improvements can expand scoring beyond exact-match checks and grow the corpus over time.
