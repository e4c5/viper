### JavaScript
- Focus on runtime safety: flag null/undefined access, async error handling, and unhandled promise flows.
- Check for race conditions and stale-state bugs in async UI/server logic.
- Flag injection risks: `eval`, dynamic code execution, unsafe template/HTML handling (XSS), and unsafe command/SQL construction.
- For React code, prioritize lifecycle/cleanup issues (missing cleanup in `useEffect`, stale closures, ref misuse), state mutation bugs, and missing key props.
- For Vue code, check for state mutation outside of mutations/actions (if using Vuex), lifecycle hooks misuse, and missing prop validation.
- For Node.js / Express code, focus on request validation, error propagation, missing rate limiting, and unsafe path construction.
