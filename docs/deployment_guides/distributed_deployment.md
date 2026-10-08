# Distributed Deployment Pre-Go-Live Checklist

- [ ] Align timeouts across load balancer, gateway, services and database.
- [ ] Size connection pools for total concurrency across all nodes.
- [ ] Verify clock synchronization and health-check intervals between nodes.
- [ ] Verify cache scale-level configuration on every node after install or upgrade.
- [ ] Run a load test at expected peak and review timeout and error rates.
- [ ] Test failover of one node while users are active.
