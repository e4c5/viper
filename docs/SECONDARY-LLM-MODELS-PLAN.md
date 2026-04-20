# Secondary LLM Models Plan

## Goal

Reduce review cost by keeping the primary review agent on the main configured model while allowing
lower-risk secondary tasks to use cheaper models:

- PR summary generation
- Finding verification

If a secondary model is not configured, the task must fall back to the main `LLM_PROVIDER`,
`LLM_MODEL`, and `LLM_API_KEY` behavior so existing deployments keep working unchanged.

## Configuration Contract

Existing primary review configuration remains the default:

```bash
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3-flash-preview
LLM_API_KEY=...
```

New optional summary configuration:

```bash
LLM_SUMMARY_PROVIDER=gemini
LLM_SUMMARY_MODEL=gemini-3-flash-lite-preview
LLM_SUMMARY_API_KEY=...
```

New optional verification configuration:

```bash
LLM_VERIFICATION_PROVIDER=gemini
LLM_VERIFICATION_MODEL=gemini-3-flash-lite-preview
LLM_VERIFICATION_API_KEY=...
```

Fallback rules:

- If `LLM_SUMMARY_PROVIDER` is unset, use `LLM_PROVIDER`.
- If `LLM_SUMMARY_MODEL` is unset, use `LLM_MODEL`.
- If `LLM_SUMMARY_API_KEY` is unset, use `LLM_API_KEY`.
- If `LLM_VERIFICATION_PROVIDER` is unset, use `LLM_PROVIDER`.
- If `LLM_VERIFICATION_MODEL` is unset, use `LLM_MODEL`.
- If `LLM_VERIFICATION_API_KEY` is unset, use `LLM_API_KEY`.

## Implementation Checklist

- [x] Add task-specific settings models in `src/code_review/config.py`.
  - [x] Add `SummaryLLMConfig` with `LLM_SUMMARY_` env prefix.
  - [x] Add `VerificationLLMConfig` with `LLM_VERIFICATION_` env prefix.
  - [x] Normalize blank task-specific API keys to unset.
  - [x] Add cached getters and include them in `reset_config_cache()`.

- [x] Add task-aware model factory helpers in `src/code_review/models.py`.
  - [x] Keep `get_configured_model()` as the primary review-model helper.
  - [x] Add a shared internal resolver that accepts a resolved provider/model/api key.
  - [x] Add `get_configured_summary_model()`.
  - [x] Add `get_configured_verification_model()`.
  - [x] Ensure task-specific API-key injection does not leave the wrong provider env var active.

- [x] Wire the summary agent to the summary model.
  - [x] Update `src/code_review/agent/summary_agent.py`.
  - [x] Keep temperature/output-token behavior unchanged initially.
  - [x] Preserve fallback to the primary model when summary env vars are unset.

- [x] Wire the verification agent to the verification model.
  - [x] Update `src/code_review/agent/verification_agent.py`.
  - [x] Keep the existing low-temperature behavior.
  - [x] Preserve fallback to the primary model when verification env vars are unset.

- [x] Update documentation.
  - [x] Add new env vars to `docs/CONFIGURATION-REFERENCE.md`.
  - [x] Add a cost-saving example to `docs/GITHUB-ACTIONS.md` or `docs/QUICKSTART.md`.
  - [x] Note that summary and verification model quality can affect summary wording and false-positive filtering.

- [x] Add tests.
  - [x] Config tests for unset secondary values falling back to primary values.
  - [x] Config tests for overriding summary provider/model/API key.
  - [x] Config tests for overriding verification provider/model/API key.
  - [x] Summary-agent test proving `create_summary_agent()` uses the summary model helper.
  - [x] Verification-agent test proving `create_verification_agent()` uses the verification model helper.
  - [x] Regression test that existing primary `get_configured_model()` behavior is unchanged.

- [x] Verify locally.
  - [x] Run focused config/model/agent tests.
  - [x] Run `python -m pytest -q tests/agent tests/models tests/config`.
  - [x] Run the full test suite if the focused tests pass.

## Suggested First Deployment

Keep the primary review model high quality:

```bash
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3-flash-preview
```

Move summary and verification to a cheaper model:

```bash
LLM_SUMMARY_PROVIDER=gemini
LLM_SUMMARY_MODEL=gemini-3-flash-lite-preview

LLM_VERIFICATION_PROVIDER=gemini
LLM_VERIFICATION_MODEL=gemini-3-flash-lite-preview
```

If the cheaper verification model rejects too many valid findings or keeps too many false positives,
move only `LLM_VERIFICATION_MODEL` back to the primary model while leaving summaries on the cheaper
model.
