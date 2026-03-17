### JavaScript/TypeScript
- Focus on runtime safety: null/undefined access, async error handling, and unhandled promise flows.
- Check for race conditions and stale-state bugs in async UI/server logic.
- Flag injection risks (`eval`, dynamic code execution, unsafe template/HTML handling, command/sql construction).
- For React/Vue code, prioritize lifecycle/cleanup issues (missing cleanup in useEffect, stale closures, ref misuse), state mutation bugs, and missing key props.
- For Node.js code, focus on request validation, error propagation, missing rate limiting, and unsafe path construction.
- For Next.js code, check server-side data-fetching correctness, missing revalidation strategy, and client/server boundary leaks.
