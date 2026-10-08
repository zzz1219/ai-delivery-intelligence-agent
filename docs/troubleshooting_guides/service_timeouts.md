# Service and Gateway Timeouts

Related root cause: `timeout`

## Typical symptoms
Requests fail with 504 or timeout errors, often at peak or in distributed environments.

## Recommended diagnostic order
1. Compare gateway, service and database timeouts for consistency.
2. Check whether backends are still processing when the gateway gives up.
3. In distributed deployments, check node-to-node latency and load balancer health checks.
4. Review recent version upgrades for changed defaults.

## Common fixes
- Align timeouts and add bounded retry with backoff.
- Scale out the overloaded service.
