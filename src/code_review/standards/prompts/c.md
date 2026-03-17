### C
- Prioritize memory safety: bounds violations, use-after-free, leaks, double free, invalid pointer arithmetic.
- Check integer overflow/underflow and signedness bugs that affect indexing, sizes, or allocations.
- Verify return-value handling for system/library calls and safe error propagation paths.
- Flag linkage/header issues only when they can cause duplicate symbol, ABI, build, or runtime defects.
