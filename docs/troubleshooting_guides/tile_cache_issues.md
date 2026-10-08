# Tile Cache and Map Latency

Related root cause: `tile_cache_misconfig`

## Typical symptoms
Map tiles are slow, blank or stale; cache hit rate drops.

## Recommended diagnostic order
1. Check cache hit rate and which scale levels miss.
2. Compare cache scale-level settings with the published map service.
3. Check whether a recent upgrade or republish reset cache settings.
4. Verify cache storage capacity and permissions.

## Common fixes
- Restore cache scale-level configuration and re-seed tiles.
- Rebuild the cache for affected levels.
