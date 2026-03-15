### Java
- Prioritize correctness and reliability: null-safety gaps, exception handling flaws, and resource leaks.
- Check thread-safety and state visibility in shared mutable objects.
- Flag API misuse that can silently fail (collections/streams, equals/hashCode contracts, Optional misuse).
- In Spring/Jakarta code, focus on validation, transaction boundaries, security annotations, and error mapping.
- Check for missing `@Transactional` rollback rules, improper lazy-loading outside of transactions, and missing `@Valid`/`@Validated` on controller inputs.
- Flag missing or overly broad exception handling in `@ControllerAdvice` and filter chains.
