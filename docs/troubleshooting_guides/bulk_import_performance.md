# Bulk Import Performance and Failures

Related root cause: `batch_size_too_large`

## Typical symptoms
Importing millions of records stalls, fails, or leaves the system slow afterwards.

## Recommended diagnostic order
1. Check the import batch size and transaction length first.
2. Check whether indexes are rebuilt after every batch.
3. Check memory and lock-wait metrics during the load.
4. After the load, verify indexes and statistics (see slow query guide).

## Common fixes
- Reduce batch size and pause index rebuilding until the load finishes.
