### JavaScript/TypeScript
- Prioritize type-safety failures that can become runtime bugs (`any` leakage, unsafe casts, unchecked unions).
- Focus on async correctness: lost promises, missing `await`, inconsistent error paths.
- Check for invalid trust of external input despite types (API payloads, request bodies, env/config values).
- For framework code, flag state/lifecycle cleanup issues and brittle typing around hooks/components/services.
- For React/Next.js code, check for missing dependency arrays in hooks, server/client component boundary misuse, and incorrect use of `use client`/`use server` directives.
- For Angular code, check for missing unsubscribe in Observables, improper change detection usage, and unsafe direct DOM manipulation.
- For Node.js/Express code, check for untyped request bodies used as trusted input and missing middleware validation.
