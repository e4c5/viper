### C++
- Focus on memory and lifetime safety: ownership confusion, dangling references/iterators, and UAF/double-free risks.
- Prefer RAII (e.g., `std::unique_ptr`, `std::lock_guard`) and established resource ownership patterns over manual cleanup.
- Check undefined behavior (UB) hazards: bounds violations, integer overflow, invalid casts (prefer `static_cast`/`dynamic_cast`), and uninitialized use.
- Flag move/copy semantics issues: missing `std::move`, unnecessary copies of large objects, and "moved-from" state misuse.
- Verify thread safety: ensure proper use of `std::mutex`, `std::atomic`, and `std::condition_variable`; flag potential races in shared state.
- Check for modern C++ best practices only when they prevent bugs or significantly improve performance (e.g., `auto` for complex types, `override` keywords).
- Missing virtual destructor in polymorphic base classes: flag class hierarchies where the base class has at least one virtual method but no virtual destructor. Deleting a derived object through a base-class pointer without a virtual destructor invokes only the base-class destructor — UB that typically causes resource leaks or heap corruption. Add `virtual ~Base() = default;` to any class intended to be subclassed and deleted polymorphically.
- `std::string::c_str()` / `data()` lifetime hazard: flag code that stores the pointer returned by `c_str()` or `data()` and then calls any non-`const` method on the owning `std::string` (or lets the string go out of scope or be reassigned). Any modification or destruction of the `std::string` object invalidates all existing character pointers; the stored pointer becomes dangling.
