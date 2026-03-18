### TypeScript
- Prioritize type-safety failures: flag `any` leakage, unsafe casts, unchecked unions, and missing type definitions for complex data.
- Focus on async correctness: lost promises, missing `await`, inconsistent error paths, and unhandled promise flows.
- Check for invalid trust of external input despite types (API payloads, request bodies, env/config values).
- For React / Next.js code, check for missing dependency arrays in hooks, server/client component boundary misuse, and incorrect use of `use client`/`use server` directives.
- For Angular code, check for missing unsubscribe in `Observables`, improper change detection usage, and unsafe direct DOM manipulation.
- For Node.js code, check for untyped request bodies used as trusted input and missing middleware validation.
