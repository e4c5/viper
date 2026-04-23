# Prompt Structure Plan For Better Caching

## Goal

Restructure review prompts so stable, reusable content appears before dynamic PR and diff content. This improves the chance that provider-side prefix caching and ADK Gemini context caching can reuse prompt tokens across batches and repeated runs.

## 1. Separate Stable And Dynamic Prompt Content

Move repeated review guidance out of `build_prepared_batch_user_message()` and into agent instructions.

Stable content should live in:

- `BATCH_EMBEDDED_DIFF_REVIEW_INSTRUCTION`
- `_BATCH_USER_MESSAGE_INSTRUCTION`
- shared line-number rules
- output schema guidance
- test-quality rules, if included globally
- provider suggestion constraints
- review standards

Dynamic content should live in user messages:

- owner/repo/pr/head SHA
- batch paths
- linked work-item context
- commit-message context
- retry warning
- diff segments

Target shape:

```text
SYSTEM / instruction:
  stable review role
  stable severity/filtering rules
  stable output schema
  stable line-number rules
  stable batch rules
  stable standards

USER:
  PR metadata
  linked context, if any
  retry note, if any
  prepared diff segments
```

## 2. Remove Repeated Rules From Batch User Messages

In `src/code_review/agent/workflows.py`, `build_prepared_batch_user_message()` currently repeats guidance like:

- "Only report findings for code that appears..."
- line annotation instructions
- JSON object instructions
- no-findings response instructions

Move these into `_BATCH_USER_MESSAGE_INSTRUCTION` or the slim batch instruction in `src/code_review/agent/agent.py`.

Afterward, `build_prepared_batch_user_message()` should mostly produce:

```text
PR:
owner=...
repo=...
pr_number=...
head_sha=...

Batch:
paths=...

Linked Work Item Context:
...

Prepared batch segments:
...
```

## 3. Move Test-Quality Rules Before Dynamic Diff

Right now `_SHARED_TEST_QUALITY_RULES` is appended after the diff only when a batch contains test files. That hurts prefix caching.

Two options:

1. Always include test-quality rules in the stable batch instruction.
   - Simpler.
   - Slightly larger prompt for non-test batches.
   - Best cache stability.

2. Create two stable sub-agent variants.
   - `batch_review_code_*`
   - `batch_review_tests_*`
   - Test batches get test rules in the instruction.
   - More complex, but keeps non-test prompts smaller.

Recommendation: choose option 1 unless prompt size becomes a real issue.

## 4. Keep Linked Context Dynamic, But Place It Before Diff

Linked context is usually PR-specific, so it will not help cross-PR caching. Within the same PR, though, it can be stable across batches.

Place it in the user message before the diff:

```text
Linked Work Item Context:
...

Prepared batch segments:
...
```

This gives providers a better chance to cache shared PR context across batch calls.

## 5. Make Batch Metadata Stable And Minimal

The current opening sentence changes immediately with owner/repo/pr/head SHA/path list. That is acceptable, but keep it small.

Avoid putting long dynamic strings before reusable context. Prefer:

```text
PR metadata:
owner=...
repo=...
pr_number=...
head_sha=...

Batch paths:
- foo.py
- bar.py
```

Then linked context, then diff.

## 6. Preserve Identical Instruction Text Across Sub-Agents

`create_sequential_batch_review_agent()` creates one sub-agent per batch. For caching, all sub-agents should have the same instruction when possible.

Avoid per-batch instruction mutations except the agent name. Specifically:

- keep `_BATCH_USER_MESSAGE_INSTRUCTION` identical
- avoid appending path-specific or retry-specific text to `agent.instruction`
- keep provider capability guidance stable for the whole run

## 7. Improve ADK Gemini Explicit Cache Behavior

ADK's Gemini cache manager caches contents before the final continuous batch of user messages. Because dynamic batch content is user content, the stable instruction is the main cacheable piece.

Concrete goal:

- stable instruction/system content should be large and identical
- dynamic batch user content should be last
- no stable rules after the diff

This helps both ADK Gemini and provider prefix caches.

## 8. Add Tests For Prompt Shape

Add tests around `build_prepared_batch_user_message()` and `create_sequential_batch_review_agent()`:

- batch user message does not contain repeated stable rules like "Only report findings..."
- batch user message contains PR metadata and diff segments
- test-quality rules are present in the agent instruction, not after diff
- two batch sub-agents have identical instruction text aside from name
- linked context still causes the linked-context instruction to be included

## 9. Add A Cacheability Regression Test

Add a test that builds two batch prompts for the same PR and checks the dynamic user messages share a meaningful prefix before the diff, for example:

```text
PR metadata
Linked Work Item Context
Prepared batch segments
```

The goal is not perfect equality, but preventing future changes from reintroducing long stable guidance after dynamic diff content.

## 10. Verify With Usage Logs

After restructuring:

- run a multi-batch review with Gemini 3+
- check `llm_usage ... cached_tokens=...`
- first batch may still show zero
- later batches should have a better chance of cache reads if ADK/provider caching can reuse the stable instruction and shared context

## First Patch

The highest-impact first patch is:

Move stable batch rules and test-quality rules out of `build_prepared_batch_user_message()` and into the batch agent instruction.
