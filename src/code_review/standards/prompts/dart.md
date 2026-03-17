### Dart / Flutter
- Focus on null safety: ensure proper use of `?`, `!`, and `late` to avoid runtime errors.
- Check async correctness: flag missing `await` in `Future` chains, improper use of `Future.sync` or `Future.value`, and unhandled `catchError` or `try-catch` blocks.
- Verify Widget lifecycle: check for missing `super.initState()`, `super.dispose()`, or `super.didUpdateWidget()` in `StatefulWidget` states.
- Flag memory leaks: ensure `StreamSubscription`, `TextEditingController`, `AnimationController`, and other `ChangeNotifier` objects are properly disposed.
- In Flutter UI code: check for expensive operations inside `build()` methods, unnecessary `setState()` calls, and missing `const` constructors where possible.
- Check state management: flag improper use of `Provider`, `Riverpod`, or `Bloc` (e.g., listening to providers inside UI build methods without `select`, or exposing private state).
- Security: flag unsafe use of `HtmlElementView` (XSS), insecure storage of sensitive data, and hardcoded API keys.
- Prefer Dart idiomatic patterns: proper use of cascading (`..`), collection `if`/`for`, and spread operators (`...`).
