### C++
- Focus on memory and lifetime safety: ownership confusion, dangling references/iterators, and UAF/double-free risks.
- Prefer RAII (e.g., `std::unique_ptr`, `std::lock_guard`) and established resource ownership patterns over manual cleanup.
- Check undefined behavior (UB) hazards: bounds violations, integer overflow, invalid casts (prefer `static_cast`/`dynamic_cast`), and uninitialized use.
- Flag move/copy semantics issues: missing `std::move`, unnecessary copies of large objects, and "moved-from" state misuse.
- Verify thread safety: ensure proper use of `std::mutex`, `std::atomic`, and `std::condition_variable`; flag potential races in shared state.
- Check for modern C++ best practices only when they prevent bugs or significantly improve performance (e.g., `auto` for complex types, `override` keywords).
