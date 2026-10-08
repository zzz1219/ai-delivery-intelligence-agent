# Map Service Publication Failures

Related root cause: `service_publish_failure`

## Typical symptoms
A map service fails to publish or starts in an error state.

## Recommended diagnostic order
1. Read the publishing log for the first error.
2. Validate the service configuration and data source connections.
3. Check the service account permissions on data and cache folders.

## Common fixes
- Republish with corrected configuration.
- Fix service account permissions.
