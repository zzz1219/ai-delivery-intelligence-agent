# Slow Queries After Large Data Loads

Related root cause: `missing_index`

## Typical symptoms
Queries or map refreshes slow down sharply after a bulk load.

## Recommended diagnostic order
1. Inspect execution plans of the slowest queries for sequential scans.
2. Verify composite and spatial indexes exist and are valid on the newly loaded tables.
3. Check that table statistics are fresh (analyze / vacuum status).
4. Compare query time before and after the load to confirm the load as the trigger.

## Common fixes
- Add a composite index on frequent filter columns.
- Rebuild the spatial index and refresh statistics.
