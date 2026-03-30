# What Stays Custom

This document records the guardrails for what should remain custom in the code-review agent, even as ADK is used more deeply for agent execution.

## Principle

ADK should own the LLM-facing execution flow.

Our Python code should continue to own product logic, SCM behavior, safety rules, and review-policy decisions.

These areas are not accidental wheel reinvention. They are the parts of the system that encode repo-specific and SCM-specific correctness.

## Keep SCM Provider Logic Outside ADK

Keep these custom:

- provider construction
- SCM authentication and API calls
- provider capability flags
- SCM-specific comment posting and fallback behavior

Where this lives now:

- `src/code_review/providers/base.py`
- `src/code_review/providers/*.py`
- `src/code_review/providers/__init__.py`

Why:

- SCM APIs differ materially across GitHub, GitLab, Gitea, Bitbucket Cloud, and Bitbucket Server/DC
- capability detection is product logic, not model logic
- posting behavior and fallbacks need deterministic handling

## Keep Idempotency and Dedupe Outside ADK

Keep these custom:

- run id construction
- existing-comment ignore sets
- fingerprint markers
- duplicate suppression

Where this lives now:

- `src/code_review/diff/fingerprint.py`
- `src/code_review/orchestration_deps.py`

Why:

- these rules must be deterministic
- they are central to avoiding duplicate or noisy review output
- they must be auditable and testable independently of model behavior

## Keep Diff Anchoring and Relocation Outside ADK

Keep these custom:

- diff parsing
- commentable position logic
- line relocation by anchor text
- fingerprint-based anchoring helpers

Where this lives now:

- `src/code_review/diff/parser.py`
- `src/code_review/diff/position.py`
- `src/code_review/orchestration_deps.py`

Why:

- anchoring correctness is SCM- and diff-format-sensitive
- the model can suggest a file and line, but relocation must remain deterministic
- bad anchoring creates visible user-facing errors

## Keep Stale-Comment Resolution Outside ADK

Keep these custom:

- deciding when a previously posted comment is now stale
- resolving or skipping stale comments based on provider capability

Where this lives now:

- `src/code_review/orchestration_deps.py`
- provider `capabilities()` and provider resolution methods

Why:

- this depends on durable markers, provider semantics, and current findings
- it must behave consistently across reruns

## Keep Review-Decision Policy Outside ADK

Keep these custom:

- severity thresholds
- quality-gate aggregation
- review-decision-only behavior
- merge-blocking policy and SCM submission rules

Where this lives now:

- `src/code_review/orchestration_deps.py`
- `src/code_review/runner.py`
- `docs/SCM-REVIEW-DECISIONS-AND-MERGE-BLOCKING.md`

Why:

- this is core product policy
- it depends on explicit thresholds and SCM semantics
- it should not drift based on prompt wording or agent state

## Practical Boundary

ADK is a good fit for:

- generating structured findings
- reply-dismissal judgment
- batch review workflow execution

ADK is not the place for:

- SCM abstraction
- idempotency
- dedupe
- anchoring and relocation
- stale-comment resolution
- review-decision policy

## Contribution Rule

Before moving any logic into ADK tools, agent state, artifacts, memory, or workflow steps, ask:

1. Is this LLM reasoning, or is it deterministic product logic?
2. Does it depend on SCM semantics or durable markers?
3. Would moving it into ADK make failures harder to reproduce or test?

If the answer points to determinism, SCM specificity, or auditability, keep it in Python.
