### C
- Prioritize memory safety: bounds violations, use-after-free, leaks, double free, and invalid pointer arithmetic.
- Check integer overflow/underflow, signedness bugs, and truncation issues that affect indexing, sizes, or allocations.
- Verify return-value handling for system/library calls (e.g., `malloc`, `read`, `write`) and ensure safe error propagation.
- Flag concurrency hazards: data races on shared globals, missing lock initialization/destruction, and deadlocks.
- Check for unsafe string functions: prefer `strncpy`/`snprintf` over `strcpy`/`sprintf` if they prevent overflows.
- Flag linkage/header issues only when they can cause duplicate symbols, ABI mismatches, or runtime defects.
