# Coordinate System Mismatch

Related root cause: `crs_mismatch`

## Typical symptoms
Layers are offset from the base map or spatial joins fail after loading data.

## Recommended diagnostic order
1. Compare the layer's spatial reference ID with the service's.
2. Check source data metadata for a missing or wrong SRID.
3. Test with a small sample before reloading the full dataset.

## Common fixes
- Reproject the layers to the service coordinate system.
- Correct the SRID metadata and reload.
