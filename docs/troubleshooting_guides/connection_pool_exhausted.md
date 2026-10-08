# Connection Pool Exhaustion

Related root cause: `connection_pool_exhausted`

## Typical symptoms
Intermittent 'cannot acquire connection' errors at peak load; recovery after restart.

## Recommended diagnostic order
1. Check pool metrics: active vs idle connections and wait time.
2. Look for code paths that do not return connections (leaks).
3. Compare pool size with peak concurrency across all nodes.
4. Review idle and acquisition timeout settings.

## Common fixes
- Increase pool size and tune timeouts.
- Patch leaking data-access paths.
