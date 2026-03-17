### Python
- Flag mutable default args, implicit `None` handling bugs, and exception swallowing.
- Verify resource safety: context managers, file/socket/db cleanup, and transaction handling.
- Check async correctness: blocking calls inside async paths, missed `await`, cancellation safety.
- Call out dangerous deserialization/eval/subprocess usage and unsafe string-built SQL/shell commands.
- Prefer type-hint clarity when it prevents bugs (public APIs, complex data flow), not as style-only noise.
- In Django code: check ORM query safety (N+1, missing select_related/prefetch_related), signal misuse, missing form validation, and unenforced permission checks.
- In FastAPI/Starlette code: check dependency injection correctness, missing request validation, missing response model enforcement, and unhandled async lifespan events.
- In Flask code: check improper use of g/session, missing input sanitization, and insecure default configs.
