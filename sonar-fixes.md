# Sonar Fixes — PR #101

**Source:** https://sonarcloud.io/dashboard?id=e4c5_viper&pullRequest=101  
**Generated:** 2026-04-22  
**Total findings:** 6 (0 security hotspots, 0 duplications)

## Summary

| Severity | Count | Rule(s) |
|----------|-------|---------|
| CRITICAL | 2 | python:S3776, python:S5797 |
| MAJOR    | 4 | python:S1172, python:S108 ×3 |

---

## 1. [CRITICAL] Cognitive Complexity too high in `_adf_link_urls`

**File:** [src/code_review/context/fetchers.py](src/code_review/context/fetchers.py#L361)  
**Rule:** `python:S3776` — Cognitive Complexity of functions should not be too high  
**Message:** Refactor this function to reduce its Cognitive Complexity from 16 to the 15 allowed.

**Verified:** `_adf_link_urls` at line 361 contains two nested loops each with nested `if` guards, accumulating 16 points of cognitive complexity. The inner logic for processing the `"attrs"` dict (for both direct attrs and mark attrs) is identical: look up a URL key, validate it's a non-empty string, append it.

**Fix:** Extract a small helper `_extract_url_from_attrs(attrs: dict | None) -> str | None` that handles the dict-check + key lookup + strip in one place. Call it from both the top-level attrs block and the marks loop. This removes two nesting levels and the repeated inline checks, dropping complexity below 15.

```python
def _extract_url_from_attrs(attrs: Any, *keys: str) -> list[str]:
    """Return non-empty string values for the given keys from an attrs dict."""
    if not isinstance(attrs, dict):
        return []
    return [v.strip() for k in keys for v in [attrs.get(k) or ""] if v.strip()]


def _adf_link_urls(node: dict[str, Any]) -> list[str]:
    urls: list[str] = _extract_url_from_attrs(node.get("attrs"), "url", "href")
    for mark in node.get("marks") or []:
        if isinstance(mark, dict):
            urls.extend(_extract_url_from_attrs(mark.get("attrs"), "href"))
    return list(dict.fromkeys(urls))
```

**Testing:** Run `pytest tests/context/` — existing unit tests for `_adf_to_plain` / Jira ADF parsing exercise `_adf_link_urls` indirectly.

---

## 2. [CRITICAL] Constant `if False:` condition in fake agent

**File:** [tests/agent/test_workflows.py](tests/agent/test_workflows.py#L27)  
**Rule:** `python:S5797` — Constants should not be used as conditions  
**Message:** Replace this expression; used as a condition it will always be constant.

**Verified:** Lines 25–28 in `_FakeReviewAgent.run_async`:
```python
async def run_async(self, ctx):
    self.seen_user_messages.append(ctx.user_content.parts[0].text)
    if False:
        yield None
```
The `if False: yield None` trick forces `run_async` to be an async-generator function (Python requires at least one `yield` syntactically), but Sonar correctly flags the constant condition.

**Fix:** Use an unreachable `yield` after an early `return` instead — this is semantically identical, avoids the constant condition, and is idiomatic:

```python
async def run_async(self, ctx):
    self.seen_user_messages.append(ctx.user_content.parts[0].text)
    return
    yield  # pragma: no cover  — makes this function an async generator
```

The `return` exits immediately; the bare `yield` is never reached but its presence in the function body is enough to make Python treat the function as an async generator.

**Testing:** `pytest tests/agent/test_workflows.py` — all existing tests must pass unchanged.

---

## 3. [MAJOR] Unused parameter `head_sha` in `create_sequential_batch_review_agent`

**File:** [src/code_review/agent/workflows.py](src/code_review/agent/workflows.py#L158)  
**Rule:** `python:S1172` — Unused function parameters should be removed  
**Message:** Remove the unused function parameter "head_sha".

**Verified:** `head_sha: str = ""` is declared at line 158 but never referenced in the function body. All callers in the codebase should be checked before removing.

**Fix:**
1. Search for all call sites of `create_sequential_batch_review_agent` and verify none pass `head_sha`.
2. Remove the `head_sha: str = ""` parameter from the function signature.
3. If the parameter was intended for future use, track it in a TODO comment or a separate issue instead of keeping a dead parameter.

```bash
grep -rn "create_sequential_batch_review_agent" src/ tests/ --include="*.py"
```

**Testing:** `pytest tests/agent/test_workflows.py tests/runner/` after removal.

---

## 4. [MAJOR] Empty `pass` block in `_FakePausableAgent.run_async`

**File:** [tests/agent/test_workflows.py](tests/agent/test_workflows.py#L181)  
**Rule:** `python:S108` — Nested blocks of code should not be left empty  
**Message:** Either remove or fill this block of code.

**Verified:** Lines 178–181:
```python
async def run_async(self, ctx):
    self.call_count += 1
    yield None
    if self.pause_on_call:
        pass  # We yield an event that causes pause
```
The `pass` does nothing — the comment is misleading (no event is yielded). The pausing behaviour is actually implemented in `should_pause_invocation`, not here.

**Fix:** Remove the dead `if self.pause_on_call: pass` block entirely, since the pause logic lives in `should_pause_invocation`. The `yield None` already makes this an async generator:

```python
async def run_async(self, ctx):
    self.call_count += 1
    yield None
```

**Testing:** `pytest tests/agent/test_workflows.py -k pausable` — the resume test should confirm `current_index` and `call_count` values are unchanged.

---

## 5. [MAJOR] Empty `pass` blocks in `async for` loops (lines 228 and 239)

**File:** [tests/agent/test_workflows.py](tests/agent/test_workflows.py#L228) and [tests/agent/test_workflows.py](tests/agent/test_workflows.py#L239)  
**Rule:** `python:S108` — Nested blocks of code should not be left empty  
**Message:** Either remove or fill this block of code.

**Verified:** Two identical patterns in `test_batch_review_workflow_resumes_from_correct_sub_agent`:
```python
async for _event in workflow._run_async_impl(ctx):
    pass
```
These exhaust the async generator to drive execution, but the empty body is flagged.

**Fix:** Replace the bare `pass` with a descriptive comment that makes the intent explicit:

```python
async for _event in workflow._run_async_impl(ctx):
    pass  # exhaust the async generator; side-effects (call_count, current_index) are what we assert
```

Sonar's S108 is satisfied when an empty block contains a non-empty comment.

**Testing:** `pytest tests/agent/test_workflows.py -k resumes_from_correct_sub_agent`.

---

## Remediation order

| # | File | Line | Rule | Effort | Action |
|---|------|------|------|--------|--------|
| 1 | `src/code_review/context/fetchers.py` | 361 | S3776 | 6 min | Extract `_extract_url_from_attrs` helper |
| 2 | `tests/agent/test_workflows.py` | 27 | S5797 | 2 min | Replace `if False: yield None` with `return; yield` |
| 3 | `src/code_review/agent/workflows.py` | 158 | S1172 | 5 min | Remove unused `head_sha` parameter |
| 4 | `tests/agent/test_workflows.py` | 181 | S108 | 5 min | Remove dead `if self.pause_on_call: pass` block |
| 5 | `tests/agent/test_workflows.py` | 228 | S108 | 5 min | Add comment to `pass` in `async for` loop |
| 6 | `tests/agent/test_workflows.py` | 239 | S108 | 5 min | Add comment to `pass` in `async for` loop |
