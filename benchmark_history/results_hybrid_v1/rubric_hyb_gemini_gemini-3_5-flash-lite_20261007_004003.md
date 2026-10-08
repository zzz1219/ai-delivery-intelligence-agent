# Hybrid rubric reading aid

Fill the `human_score` column: 1 = yes, 0 = no for every item; `unsupported_claims` is a count.
sqlok: look at the recorded SQL task and decide whether it really computes the quantity (the machine note is only a screening).
Judge ONLY against the SQL results and documents shown below. Do not use your own knowledge.

## H1
**Question:** Among closed incidents, are database incidents slower to resolve on average than non-database incidents? Across closed incidents, which root cause has the highest mean resolution time, and what does its troubleshooting guide recommend checking first?

**Outcome:** ok

**Plan:**
```json
{
 "requires_sql": true,
 "requires_retrieval": true,
 "retrieval_depends_on_sql": true,
 "retrieval_purpose": "Look up the troubleshooting guide for the root cause with the highest mean resolution time to see what it recommends checking first."
}
```

**SQL task `t1`** (ok): Calculate the average resolution time for closed database incidents versus closed non-database incidents.
```sql
SELECT 
    CASE 
        WHEN c.layer = 'database' THEN 'database'
        ELSE 'non-database'
    END AS incident_type,
    AVG((julianday(i.resolved_at) - julianday(i.reported_at)) * 24) AS avg_resolution_time_hours
FROM incidents i
JOIN components c ON i.component_id = c.component_id
WHERE i.status = 'closed'
GROUP BY 
    CASE 
        WHEN c.layer = 'database' THEN 'database'
        ELSE 'non-database'
    END
```
```
incident_type | avg_resolution_time_hours
database | 38.42820512904571
non-database | 16.741780821897397
```

**SQL task `t2`** (ok): Find the mean resolution time for closed incidents grouped by root cause to identify the root cause with the highest mean resolution time.
```sql
SELECT 
    root_cause, 
    AVG((julianday(resolved_at) - julianday(reported_at)) * 24) AS mean_resolution_time_hours
FROM incidents
WHERE status = 'closed'
GROUP BY root_cause
ORDER BY mean_resolution_time_hours DESC
LIMIT 1
```
```
root_cause | mean_resolution_time_hours
connection_pool_exhausted | 41.07333333268762
```

**Retrieval queries written by the model** (query status: ok): ["connection_pool_exhausted troubleshooting guide check first"]

**Documents the model saw:** historical_cases/INC_0005, troubleshooting_guides/connection_pool_exhausted, troubleshooting_guides/bulk_import_performance

<details><summary>historical_cases/INC_0005</summary>

```
# Incident Case INC_0005

- Incident ID: INC_0005
- Project: Land Survey Management System (P02), government sector
- Component: Primary Database (C01)
- Environment: cloud, version v3.0
- Severity: critical
- Reported: 2025-10-13 08:02:00
- Resolved: 2025-10-16 12:44:00 (76.7 hours)
- Root cause: connection_pool_exhausted
- Solution pattern: increase_pool_size_and_timeouts

## Symptoms
Users saw intermittent 'cannot acquire connection' errors during peak hours; response time climbed until restart.

## Investigation
Pool metrics showed active connections pinned at the maximum while idle time was near zero; some connections were never returned.

## Root Cause
Intermittent failures under load were traced to connection pool exhaustion.

## Resolution
Increased the connection pool size and tuned idle and acquisition timeouts.

## Lessons Learned
Size pools for peak concurrency and alert on pool saturation.

_Synthetic case generated from the incidents table for portfolio use._
```

</details>

<details><summary>troubleshooting_guides/connection_pool_exhausted</summary>

```
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
```

</details>

<details><summary>troubleshooting_guides/bulk_import_performance</summary>

```
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
```

</details>

**Answer:**

Yes, database incidents are slower to resolve on average (38.43 hours) than non-database incidents (16.74 hours) [sql:t1]. 

Across closed incidents, the root cause with the highest mean resolution time is `connection_pool_exhausted` with a mean resolution time of 41.07 hours [sql:t2]. 

For this root cause, the troubleshooting guide recommends checking pool metrics (active vs. idle connections and wait time) first [troubleshooting_guides/connection_pool_exhausted].

**Machine flags:**
- invalid document citations: []; invalid sql citations: []
- numbers in the answer that no SQL result or document supports: []

**Items:**
- `sqlok:H1-s1`: SQL stage: one recorded SQL task correctly computes this quantity: The mean resolution time of closed database incidents is 38.4 hours.
- `says:H1-s1`: The answer states this correctly: The mean resolution time of closed database incidents is 38.4 hours.
- `sqlok:H1-s2`: SQL stage: one recorded SQL task correctly computes this quantity: The mean resolution time of closed non-database incidents is 16.7 hours.
- `says:H1-s2`: The answer states this correctly: The mean resolution time of closed non-database incidents is 16.7 hours.
- `sqlok:H1-s3`: SQL stage: one recorded SQL task correctly computes this quantity: Database incidents are slower to resolve than non-database incidents.
- `says:H1-s3`: The answer states this correctly: Database incidents are slower to resolve than non-database incidents.
- `sqlok:H1-s4`: SQL stage: one recorded SQL task correctly computes this quantity: The root cause with the highest mean resolution time is connection_pool_exhausted.
- `says:H1-s4`: The answer states this correctly: The root cause with the highest mean resolution time is connection_pool_exhausted.
- `sqlok:H1-s5`: SQL stage: one recorded SQL task correctly computes this quantity: Its mean resolution time is 41.1 hours.
- `says:H1-s5`: The answer states this correctly: Its mean resolution time is 41.1 hours.
- `covered:H1-d1`: The answer covers: Its troubleshooting guide recommends checking first: Check pool metrics: active vs idle connections and wait time.
- `unsupported_claims`: Number of statements in the answer that the SQL results and the provided documents do not support

## H2
**Question:** Define a repeat incident as a closed incident for which at least one other closed incident exists with the same project, component and root cause within 90 days (before or after it). Under this definition, which root cause has the most repeat incidents, and what lesson learned is recorded in similar historical cases?

**Outcome:** ok

**Plan:**
```json
{
 "requires_sql": true,
 "requires_retrieval": true,
 "retrieval_depends_on_sql": true,
 "retrieval_purpose": "Look up historical incident case reports and lessons learned for the specific root cause identified by the SQL query as having the most repeat incidents."
}
```

**SQL task `t1`** (ok): Identify all closed incidents with their project, component, root cause, and incident dates, then find pairs of closed incidents for the same project, component, and root cause where the incident dates are within 90 days of each other, and aggregate the count of repeat incidents per root cause to find the root cause with the most repeat incidents.
```sql
WITH closed_incidents AS (
    SELECT 
        i.incident_id,
        i.project_id,
        i.component_id,
        i.root_cause,
        i.reported_at
    FROM incidents i
    WHERE i.status = 'closed'
    AND i.root_cause IS NOT NULL
),
repeat_pairs AS (
    SELECT 
        i1.root_cause
    FROM closed_incidents i1
    JOIN closed_incidents i2 
        ON i1.project_id = i2.project_id
        AND i1.component_id = i2.component_id
        AND i1.root_cause = i2.root_cause
        AND i1.incident_id < i2.incident_id
        AND julianday(i2.reported_at) >= julianday(i1.reported_at)
        AND julianday(i2.reported_at) <= julianday(i1.reported_at) + 90
)
SELECT 
    root_cause,
    COUNT(*) AS repeat_incident_count
FROM repeat_pairs
GROUP BY root_cause
ORDER BY repeat_incident_count DESC
LIMIT 1
```
```
root_cause | repeat_incident_count
crs_mismatch | 37
```

**Retrieval queries written by the model** (query status: ok): ["historical incident case reports lessons learned crs_mismatch", "crs_mismatch root cause troubleshooting guide lessons learned"]

**Documents the model saw:** historical_cases/INC_0015, troubleshooting_guides/crs_mismatch, troubleshooting_guides/bulk_import_performance

<details><summary>historical_cases/INC_0015</summary>

```
# Incident Case INC_0015

- Incident ID: INC_0015
- Project: Land Survey Management System (P02), government sector
- Component: Map Service (C03)
- Environment: cloud, version v3.0
- Severity: low
- Reported: 2025-11-07 11:26:00
- Resolved: 2025-11-07 22:50:00 (11.4 hours)
- Root cause: crs_mismatch
- Solution pattern: reproject_to_service_crs

## Symptoms
Newly loaded layers appeared offset from the base map by hundreds of meters.

## Investigation
Comparing layer metadata against the service showed different spatial reference identifiers.

## Root Cause
Misplaced or unjoinable features were traced to a coordinate system mismatch.

## Resolution
Reprojected the loaded layers to the service coordinate system.

## Lessons Learned
Validate the spatial reference of incoming data before loading.

_Synthetic case generated from the incidents table for portfolio use._
```

</details>

<details><summary>troubleshooting_guides/crs_mismatch</summary>

```
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
```

</details>

<details><summary>troubleshooting_guides/bulk_import_performance</summary>

```
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
```

</details>

**Answer:**

Based on the SQL analysis, the root cause with the most repeat incidents is `crs_mismatch` with 37 repeat incidents [sql:t1]. 

For this root cause, symptomatically similar historical cases record the following lesson learned: validate the spatial reference of incoming data before loading [historical_cases/INC_0015].

**Machine flags:**
- invalid document citations: []; invalid sql citations: []
- numbers in the answer that no SQL result or document supports: []

**Items:**
- `sqlok:H2-s1`: SQL stage: one recorded SQL task correctly computes this quantity: The root cause with the most repeat incidents is timeout.
- `says:H2-s1`: The answer states this correctly: The root cause with the most repeat incidents is timeout.
- `sqlok:H2-s2`: SQL stage: one recorded SQL task correctly computes this quantity: It has 25 repeat incidents.
- `says:H2-s2`: The answer states this correctly: It has 25 repeat incidents.
- `covered:H2-d1`: The answer covers: The lesson learned recorded in similar historical cases: Keep timeouts consistent across gateway, services and database.
- `unsupported_claims`: Number of statements in the answer that the SQL results and the provided documents do not support

## H3
**Question:** For closed critical incidents in distributed deployments, what is the mean resolution time and how many such incidents are there? What is the most common root cause among them, and what does its troubleshooting guide recommend checking first?

**Outcome:** ok

**Plan:**
```json
{
 "requires_sql": true,
 "requires_retrieval": true,
 "retrieval_depends_on_sql": true,
 "retrieval_purpose": "Look up the troubleshooting guide for the most common root cause of critical distributed incidents to find what it recommends checking first."
}
```

**SQL task `t1`** (ok): Calculate the mean resolution time and total count of closed critical incidents in distributed deployments.
```sql
SELECT 
    AVG((julianday(i.resolved_at) - julianday(i.reported_at)) * 24) AS mean_resolution_time_hours,
    COUNT(i.incident_id) AS total_closed_critical_incidents
FROM incidents i
JOIN deployments d ON i.deployment_id = d.deployment_id
WHERE i.status = 'closed'
  AND i.severity = 'critical'
  AND d.env_type = 'distributed'
```
```
mean_resolution_time_hours | total_closed_critical_incidents
35.43749999720603 | 8
```

**SQL task `t2`** (ok): Find the most common root cause among closed critical incidents in distributed deployments.
```sql
SELECT i.root_cause, COUNT(*) AS incident_count
FROM incidents i
JOIN deployments d ON i.deployment_id = d.deployment_id
WHERE i.status = 'closed'
  AND i.severity = 'critical'
  AND d.env_type = 'distributed'
  AND i.root_cause IS NOT NULL
GROUP BY i.root_cause
ORDER BY incident_count DESC
LIMIT 1
```
```
root_cause | incident_count
timeout | 4
```

**Retrieval queries written by the model** (query status: ok): ["timeout troubleshooting guide check first"]

**Documents the model saw:** historical_cases/INC_0054, troubleshooting_guides/bulk_import_performance, troubleshooting_guides/service_timeouts

<details><summary>historical_cases/INC_0054</summary>

```
# Incident Case INC_0054

- Incident ID: INC_0054
- Project: Port Operations Map (P09), transport sector
- Component: Tile Cache (C04)
- Environment: distributed, version v3.2
- Severity: medium
- Reported: 2026-02-06 10:01:00
- Resolved: 2026-02-07 06:55:00 (20.9 hours)
- Root cause: timeout
- Solution pattern: increase_gateway_timeout_and_retry

## Symptoms
Users reported long waits followed by timeout errors in the distributed environment.

## Investigation
Gateway and backend logs showed requests exceeding the configured timeout while upstream services were still processing. The problem first appeared shortly after the upgrade to v3.2.

## Root Cause
Intermittent request failures were traced to mismatched or too-short timeouts.

## Resolution
Raised gateway timeouts and added bounded retry with backoff.

## Lessons Learned
Keep timeouts consistent across gateway, services and database.

_Synthetic case generated from the incidents table for portfolio use._
```

</details>

<details><summary>troubleshooting_guides/bulk_import_performance</summary>

```
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
```

</details>

<details><summary>troubleshooting_guides/service_timeouts</summary>

```
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
```

</details>

**Answer:**

For closed critical incidents in distributed deployments, there are 8 such incidents [sql:t1] with a mean resolution time of approximately 35.44 hours [sql:t1]. The most common root cause among them is timeout, accounting for 4 incidents [sql:t2]. 

For this root cause, the troubleshooting guide recommends checking the following first:
1. Compare gateway, service and database timeouts for consistency [troubleshooting_guides/service_timeouts].
2. Check whether backends are still processing when the gateway gives up [troubleshooting_guides/service_timeouts].
3. In distributed deployments, check node-to-node latency and load balancer health checks [troubleshooting_guides/service_timeouts].
4. Review recent version upgrades for changed defaults [troubleshooting_guides/service_timeouts].

Additionally, historical case `historical_cases/INC_0054` describes a symptomatically similar precedent where users experienced long waits followed by timeout errors in a distributed environment [historical_cases/INC_0054].

**Machine flags:**
- invalid document citations: []; invalid sql citations: []
- numbers in the answer that no SQL result or document supports: []

**Items:**
- `sqlok:H3-s1`: SQL stage: one recorded SQL task correctly computes this quantity: The mean resolution time is 35.4 hours.
- `says:H3-s1`: The answer states this correctly: The mean resolution time is 35.4 hours.
- `sqlok:H3-s2`: SQL stage: one recorded SQL task correctly computes this quantity: There are 8 such incidents.
- `says:H3-s2`: The answer states this correctly: There are 8 such incidents.
- `sqlok:H3-s3`: SQL stage: one recorded SQL task correctly computes this quantity: The most common root cause among them is timeout.
- `says:H3-s3`: The answer states this correctly: The most common root cause among them is timeout.
- `sqlok:H3-s4`: SQL stage: one recorded SQL task correctly computes this quantity: It accounts for 4 of them.
- `says:H3-s4`: The answer states this correctly: It accounts for 4 of them.
- `covered:H3-d1`: The answer covers: Its troubleshooting guide recommends checking first: Compare gateway, service and database timeouts for consistency.
- `unsupported_claims`: Number of statements in the answer that the SQL results and the provided documents do not support

## H4
**Question:** Did map-service incidents spike in 2026 Q1 compared with the surrounding quarters? Which deployment version was associated with most of the Q1 incidents, does the available evidence establish that this version caused the spike, and what concrete failure mode is documented in at least one relevant historical case?

**Outcome:** ok

**Plan:**
```json
{
 "requires_sql": true,
 "requires_retrieval": true,
 "retrieval_depends_on_sql": true,
 "retrieval_purpose": "Look up historical case reports or post-mortems for the identified map-service deployment version in 2026 Q1 to check if it caused the spike and to identify the concrete documented failure mode."
}
```

**SQL task `t1`** (ok): Calculate the total number of incidents for the map-service component per quarter for 2025 Q4, 2026 Q1, and 2026 Q2.
```sql
SELECT 
    CASE 
        WHEN reported_at >= '2025-10-01' AND reported_at < '2026-01-01' THEN '2025 Q4'
        WHEN reported_at >= '2026-01-01' AND reported_at < '2026-04-01' THEN '2026 Q1'
        WHEN reported_at >= '2026-04-01' AND reported_at < '2026-07-01' THEN '2026 Q2'
    END AS quarter,
    COUNT(i.incident_id) AS total_incidents
FROM incidents i
JOIN components c ON i.component_id = c.component_id
WHERE c.layer = 'map_service'
  AND reported_at >= '2025-10-01' AND reported_at < '2026-07-01'
GROUP BY quarter
ORDER BY quarter
```
```
quarter | total_incidents
2025 Q4 | 11
2026 Q1 | 40
2026 Q2 | 11
```

**SQL task `t2`** (ok): Find the deployment versions associated with map-service incidents during 2026 Q1, grouped by version and ordered by incident count.
```sql
SELECT 
    d.version,
    COUNT(i.incident_id) AS incident_count
FROM incidents i
JOIN deployments d ON i.deployment_id = d.deployment_id
WHERE (i.category = 'map_service' OR i.component_id IN (SELECT component_id FROM components WHERE layer = 'map_service'))
  AND i.reported_at >= '2026-01-01 00:00:00'
  AND i.reported_at < '2026-04-01 00:00:00'
GROUP BY d.version
ORDER BY incident_count DESC
```
```
version | incident_count
v3.2 | 32
v3.1 | 8
```

**Retrieval queries written by the model** (query status: ok): ["map-service v3.2 incident root cause 2026 Q1 post-mortem", "map-service v3.2 deployment failure mode incidents"]

**Documents the model saw:** historical_cases/INC_0070, historical_cases/INC_0075, troubleshooting_guides/service_publication_failures, troubleshooting_guides/tile_cache_issues, deployment_guides/single_node_deployment

<details><summary>historical_cases/INC_0070</summary>

```
# Incident Case INC_0070

- Incident ID: INC_0070
- Project: Land Survey Management System (P02), government sector
- Component: Map Service (C03)
- Environment: distributed, version v3.2
- Severity: low
- Reported: 2026-02-25 12:24:00
- Resolved: 2026-02-26 07:36:00 (19.2 hours)
- Root cause: tile_cache_misconfig
- Solution pattern: adjust_cache_scale_levels

## Symptoms
Map tiles loaded slowly and some zoom levels rendered blank or stale.

## Investigation
Cache statistics showed a very low hit rate; scale-level settings did not match the published service. The v3.2 upgrade reset the cache scale-level settings to defaults.

## Root Cause
Slow or stale map tiles were traced to a tile cache misconfiguration.

## Resolution
Restored the cache scale-level configuration and re-seeded tiles.

## Lessons Learned
Verify cache scale levels after every upgrade or service republish.

_Synthetic case generated from the incidents table for portfolio use._
```

</details>

<details><summary>historical_cases/INC_0075</summary>

```
# Incident Case INC_0075

- Incident ID: INC_0075
- Project: Land Survey Management System (P02), government sector
- Component: API Gateway (C07)
- Environment: distributed, version v3.2
- Severity: low
- Reported: 2026-03-04 14:16:00
- Resolved: 2026-03-05 00:10:00 (9.9 hours)
- Root cause: authentication_failure
- Solution pattern: fix_sso_mapping

## Symptoms
Token validation failed intermittently and blocked access to several modules.

## Investigation
Auth service logs showed token signature or attribute-mapping errors correlated with the reported failures.

## Root Cause
Login or token failures were traced to authentication configuration.

## Resolution
Fixed the SSO attribute mapping.

## Lessons Learned
Test SSO and token settings in staging before changes go live.

_Synthetic case generated from the incidents table for portfolio use._
```

</details>

<details><summary>troubleshooting_guides/service_publication_failures</summary>

```
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
```

</details>

<details><summary>troubleshooting_guides/tile_cache_issues</summary>

```
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
```

</details>

<details><summary>deployment_guides/single_node_deployment</summary>

```
# Single-Node Deployment Checklist

- [ ] Confirm database, map service and cache share enough memory and disk.
- [ ] Set connection pool size within database limits.
- [ ] Run a smoke test of import, query and map rendering.
- [ ] Verify cache scale levels match the published service.
```

</details>

**Answer:**

Map-service incidents did spike in 2026 Q1, totaling 40 incidents compared to 11 in 2025 Q4 and 11 in 2026 Q2 [sql:t1]. 

Deployment version v3.2 was associated with the majority of the Q1 incidents (32 incidents), while version v3.1 was associated with 8 [sql:t2]. However, the available evidence does not establish that version v3.2 caused the spike, as counts and co-occurrence only show an association rather than causation. 

A symptomatically similar historical case (`historical_cases/INC_0070`) documents a concrete failure mode where map tiles loaded slowly and some zoom levels rendered blank or stale due to a tile cache misconfiguration (`tile_cache_misconfig`), where scale-level settings did not match the published service after an upgrade [historical_cases/INC_0070].

**Machine flags:**
- invalid document citations: []; invalid sql citations: []
- numbers in the answer that no SQL result or document supports: []

**Items:**
- `sqlok:H4-s1`: SQL stage: one recorded SQL task correctly computes this quantity: Map-service incidents in 2025-Q4: 11.
- `says:H4-s1`: The answer states this correctly: Map-service incidents in 2025-Q4: 11.
- `sqlok:H4-s2`: SQL stage: one recorded SQL task correctly computes this quantity: Map-service incidents in 2026-Q1: 40.
- `says:H4-s2`: The answer states this correctly: Map-service incidents in 2026-Q1: 40.
- `sqlok:H4-s3`: SQL stage: one recorded SQL task correctly computes this quantity: Map-service incidents in 2026-Q2: 11.
- `says:H4-s3`: The answer states this correctly: Map-service incidents in 2026-Q2: 11.
- `sqlok:H4-s4`: SQL stage: one recorded SQL task correctly computes this quantity: Map-service incidents in 2026-Q3: 14.
- `says:H4-s4`: The answer states this correctly: Map-service incidents in 2026-Q3: 14.
- `sqlok:H4-s5`: SQL stage: one recorded SQL task correctly computes this quantity: The version associated with most Q1 incidents is v3.2.
- `says:H4-s5`: The answer states this correctly: The version associated with most Q1 incidents is v3.2.
- `sqlok:H4-s6`: SQL stage: one recorded SQL task correctly computes this quantity: v3.2 accounts for 32 of the 40 Q1 incidents.
- `says:H4-s6`: The answer states this correctly: v3.2 accounts for 32 of the 40 Q1 incidents.
- `covered:H4-d1`: The answer covers: At least one documented map-service case from 2026 Q1 describes a concrete failure mode (for example tile-cache scale levels reset to defaults, request timeouts, or a coordinate-system mismatch).
- `req:H4-y1`: The answer says the spike coincides with / is associated with the version.
- `req:H4-y2`: The answer does not present the version as the established cause of the spike (the incident counts alone establish association, not causation); citing a documented mechanism in individual cases as a plausible explanation is allowed.
- `unsupported_claims`: Number of statements in the answer that the SQL results and the provided documents do not support

## H5
**Question:** Which project currently has the most open incidents? For that project's open high-severity incidents in the map-service layer, have we seen similar cases before, and if so which?

**Outcome:** ok

**Plan:**
```json
{
 "requires_sql": true,
 "requires_retrieval": true,
 "retrieval_depends_on_sql": true,
 "retrieval_purpose": "Search historical incident case reports and troubleshooting guides for past cases similar to the open high-severity incidents in the map-service layer of the identified project."
}
```

**SQL task `t1`** (ok): Identify the project with the currently highest count of open incidents.
```sql
SELECT 
    p.project_id,
    p.name,
    COUNT(i.incident_id) AS open_incident_count
FROM projects p
JOIN incidents i ON p.project_id = i.project_id
WHERE i.status = 'open'
GROUP BY p.project_id, p.name
ORDER BY open_incident_count DESC
LIMIT 1
```
```
project_id | name | open_incident_count
P03 | Grid Asset Mapping | 9
```

**SQL task `t2`** (ok): List the open high-severity incidents for the identified project that are associated with the map-service layer, including their descriptions and symptoms.
```sql
SELECT 
    i.incident_id,
    i.project_id,
    i.symptom_summary,
    i.resolution_summary
FROM incidents i
JOIN components c ON i.component_id = c.component_id
WHERE i.status = 'open'
  AND i.severity = 'high'
  AND (c.layer = 'map_service' OR i.category = 'map_service')
ORDER BY i.reported_at
```
```
incident_id | project_id | symptom_summary | resolution_summary
INC_0185 | P03 | Map latency increased and tiles keep being regenerated instead of served from cache. | None
INC_0188 | P03 | Map service cannot be published or starts in an error state after a configuration change. | None
INC_0192 | P03 | Data loads fine but features appear in the wrong place. | None
INC_0200 | P03 | Publishing the map service fails and the service stays unavailable. | None
```

**Retrieval queries written by the model** (query status: ok): ["Grid Asset Mapping map latency tile regeneration cache", "map service error state configuration change publishing fails", "data loads fine features wrong place map service"]

**Documents the model saw:** historical_cases/INC_0028, historical_cases/INC_0016, historical_cases/INC_0049, troubleshooting_guides/tile_cache_issues, troubleshooting_guides/authentication_failures, troubleshooting_guides/service_publication_failures, troubleshooting_guides/crs_mismatch

<details><summary>historical_cases/INC_0028</summary>

```
# Incident Case INC_0028

- Incident ID: INC_0028
- Project: Grid Asset Mapping (P03), energy sector
- Component: Tile Cache (C04)
- Environment: distributed, version v3.0
- Severity: low
- Reported: 2025-11-24 16:31:00
- Resolved: 2025-11-25 08:07:00 (15.6 hours)
- Root cause: timeout
- Solution pattern: tune_query_timeouts

## Symptoms
Requests through the Tile Cache intermittently failed with 504 timeouts, mostly at peak usage.

## Investigation
Gateway and backend logs showed requests exceeding the configured timeout while upstream services were still processing.

## Root Cause
Intermittent request failures were traced to mismatched or too-short timeouts.

## Resolution
Aligned query and gateway timeouts and optimized the slowest queries.

## Lessons Learned
Keep timeouts consistent across gateway, services and database.

_Synthetic case generated from the incidents table for portfolio use._
```

</details>

<details><summary>historical_cases/INC_0016</summary>

```
# Incident Case INC_0016

- Incident ID: INC_0016
- Project: Resource Census Platform (P06), government sector
- Component: Web Frontend (C06)
- Environment: single node, version v3.1
- Severity: medium
- Reported: 2025-11-07 11:28:00
- Resolved: 2025-11-07 19:40:00 (8.2 hours)
- Root cause: frontend_config_error
- Solution pattern: correct_frontend_config

## Symptoms
Some pages failed to load after configuration was updated for the single node environment.

## Investigation
Browser network traces showed requests going to an outdated endpoint.

## Root Cause
Blank or failing pages were traced to a frontend configuration error.

## Resolution
Corrected the frontend endpoint configuration.

## Lessons Learned
Keep environment-specific configuration under version control.

_Synthetic case generated from the incidents table for portfolio use._
```

</details>

<details><summary>historical_cases/INC_0049</summary>

```
# Incident Case INC_0049

- Incident ID: INC_0049
- Project: Resource Census Platform (P06), government sector
- Component: Map Service (C03)
- Environment: single node, version v3.1
- Severity: high
- Reported: 2026-02-01 08:31:00
- Resolved: 2026-02-01 22:55:00 (14.4 hours)
- Root cause: crs_mismatch
- Solution pattern: reproject_to_service_crs

## Symptoms
The data load completed but features displayed in the wrong location or failed spatial joins.

## Investigation
Comparing layer metadata against the service showed different spatial reference identifiers.

## Root Cause
Misplaced or unjoinable features were traced to a coordinate system mismatch.

## Resolution
Reprojected the loaded layers to the service coordinate system.

## Lessons Learned
Validate the spatial reference of incoming data before loading.

_Synthetic case generated from the incidents table for portfolio use._
```

</details>

<details><summary>troubleshooting_guides/tile_cache_issues</summary>

```
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
```

</details>

<details><summary>troubleshooting_guides/authentication_failures</summary>

```
# Authentication and SSO Failures

Related root cause: `authentication_failure`

## Typical symptoms
Users are redirected to login, get 401 errors, or tokens fail validation.

## Recommended diagnostic order
1. Check auth service logs for signature or mapping errors.
2. Verify token validity settings and signing keys.
3. Compare SSO attribute mapping with the identity provider.

## Common fixes
- Correct token settings and refresh keys.
- Fix the SSO attribute mapping.
```

</details>

<details><summary>troubleshooting_guides/service_publication_failures</summary>

```
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
```

</details>

<details><summary>troubleshooting_guides/crs_mismatch</summary>

```
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
```

</details>

**Answer:**

The project with the currently highest count of open incidents is Grid Asset Mapping (P03) with 9 open incidents [sql:t1]. 

For project P03, the open high-severity incidents in the map-service layer are INC_0185, INC_0188, INC_0192, and INC_0200 [sql:t2]. 

Regarding similar past cases:
- For INC_0192 ("Data loads fine but features appear in the wrong place" [sql:t2]), a symptomatically similar precedent is historical case INC_0049, where features displayed in the wrong location due to a coordinate system mismatch [historical_cases/INC_0049]. 
- For the other open high-severity map-service incidents (INC_0185, INC_0188, and INC_0200) [sql:t2], no documented similar case was found in the historical records.

**Machine flags:**
- invalid document citations: []; invalid sql citations: []
- numbers in the answer that no SQL result or document supports: []

**Items:**
- `sqlok:H5-s1`: SQL stage: one recorded SQL task correctly computes this quantity: The project with the most open incidents is P03.
- `says:H5-s1`: The answer states this correctly: The project with the most open incidents is P03.
- `sqlok:H5-s2`: SQL stage: one recorded SQL task correctly computes this quantity: It has 9 open incidents.
- `says:H5-s2`: The answer states this correctly: It has 9 open incidents.
- `sqlok:H5-s3`: SQL stage: one recorded SQL task correctly computes this quantity: Its open high-severity incidents in the map-service layer are: INC_0185, INC_0188, INC_0192, INC_0200.
- `says:H5-s3`: The answer states this correctly: Its open high-severity incidents in the map-service layer are: INC_0185, INC_0188, INC_0192, INC_0200.
- `covered:H5-d-INC_0185`: The answer covers: For INC_0185 a documented similar historical case exists (same failure mode).
- `covered:H5-d-INC_0192`: The answer covers: For INC_0192 a documented similar historical case exists (same failure mode).
- `req:H5-y-INC_0188`: For INC_0188 the answer states that no documented similar case was found and does not present an unrelated case as a precedent or assert a root cause.
- `req:H5-y-INC_0200`: For INC_0200 the answer states that no documented similar case was found and does not present an unrelated case as a precedent or assert a root cause.
- `unsupported_claims`: Number of statements in the answer that the SQL results and the provided documents do not support
