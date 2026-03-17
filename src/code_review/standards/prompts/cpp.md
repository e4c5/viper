### C++
- Focus on memory and lifetime safety: ownership confusion, dangling references, UAF/double free risks.
- Prefer RAII/resource ownership fixes over manual cleanup patterns.
- Check undefined behavior hazards (bounds, integer overflow, invalid casts, uninitialized use).
- Flag move/copy semantics issues only when they can cause bugs, leaks, or major performance regressions.
