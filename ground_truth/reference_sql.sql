-- S1_most_critical_incidents_last_quarter
-- reference date 2026-10-05 => last quarter = 2026-Q3
SELECT project_id, COUNT(*) AS critical_incidents FROM incidents
WHERE severity='critical' AND reported_at >= '2026-07-01' AND reported_at < '2026-10-01'
GROUP BY project_id ORDER BY critical_incidents DESC;

-- S2_avg_resolution_hours_by_category
SELECT category, ROUND(AVG((julianday(resolved_at)-julianday(reported_at))*24),1) AS avg_hours
FROM incidents WHERE status='closed' GROUP BY category ORDER BY avg_hours DESC;

-- S3_map_service_monthly_trend
SELECT strftime('%Y-%m', reported_at) AS month, COUNT(*) AS incidents FROM incidents
WHERE category='map_service' GROUP BY month ORDER BY month;

-- S4_timeouts_per_deployment_by_env
-- observed timeout incidents per deployment (root_cause is known only for closed incidents);
-- this is NOT a failure rate: deployments have different exposure periods.
SELECT d.env_type,
  ROUND(1.0*SUM(CASE WHEN i.root_cause='timeout' THEN 1 ELSE 0 END)/COUNT(DISTINCT d.deployment_id),2)
    AS timeout_incidents_per_deployment
FROM deployments d LEFT JOIN incidents i ON i.deployment_id=d.deployment_id
GROUP BY d.env_type ORDER BY 2 DESC;

-- S5_open_share_by_severity
SELECT severity, ROUND(100.0*SUM(status='open')/COUNT(*),1) AS pct_open FROM incidents GROUP BY severity;

-- H1_resolution_hours_database_vs_others
SELECT CASE WHEN category='database' THEN 'database' ELSE 'other' END AS grp,
  ROUND(AVG((julianday(resolved_at)-julianday(reported_at))*24),1) AS avg_hours
FROM incidents WHERE status='closed' GROUP BY grp;

-- H2_repeat_incidents_by_component
-- repeat = same project_id + component_id + root_cause within 90 days (closed incidents only,
-- because root_cause is NULL for open incidents)
WITH repeats AS (
  SELECT a.incident_id AS id, a.component_id FROM incidents a JOIN incidents b
    ON a.project_id=b.project_id AND a.component_id=b.component_id
   AND a.root_cause=b.root_cause AND a.incident_id<>b.incident_id
   AND ABS(julianday(a.reported_at)-julianday(b.reported_at))<=90)
SELECT component_id, COUNT(DISTINCT id) AS repeat_incidents FROM repeats
GROUP BY component_id ORDER BY repeat_incidents DESC;

-- H2b_repeat_incident_details
-- parameter: :component_id
WITH repeats AS (
  SELECT DISTINCT a.incident_id AS id FROM incidents a JOIN incidents b
    ON a.project_id=b.project_id AND a.component_id=b.component_id
   AND a.root_cause=b.root_cause AND a.incident_id<>b.incident_id
   AND ABS(julianday(a.reported_at)-julianday(b.reported_at))<=90
  WHERE a.component_id=:component_id)
SELECT i.incident_id, i.project_id, i.root_cause, i.solution_pattern FROM incidents i
JOIN repeats r ON r.id=i.incident_id ORDER BY i.incident_id;

-- H3_critical_in_distributed
SELECT ROUND(AVG((julianday(i.resolved_at)-julianday(i.reported_at))*24),1) AS avg_hours,
  COUNT(*) AS n FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.severity='critical' AND d.env_type='distributed' AND i.status='closed';

-- H3b_fix_counts_critical_distributed
SELECT i.solution_pattern, COUNT(*) AS n FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.severity='critical' AND d.env_type='distributed' AND i.status='closed'
GROUP BY i.solution_pattern ORDER BY n DESC;

-- H4_map_service_by_quarter
SELECT strftime('%Y',reported_at)||'-Q'||((CAST(strftime('%m',reported_at) AS INTEGER)+2)/3) AS quarter,
  COUNT(*) AS incidents FROM incidents WHERE category='map_service' GROUP BY quarter ORDER BY quarter;

-- H4b_q1_map_service_by_version
-- the spike may be described as "coincided with v3.2" only if v3.2 holds >= 80% of these incidents
SELECT d.version, COUNT(*) AS incidents FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.category='map_service' AND i.reported_at >= '2026-01-01' AND i.reported_at < '2026-04-01'
GROUP BY d.version ORDER BY incidents DESC;

-- H5_open_incidents_by_project
SELECT project_id, COUNT(*) AS open_incidents FROM incidents WHERE status='open'
GROUP BY project_id ORDER BY open_incidents DESC;

-- H5b_open_incidents_of_project
-- parameter: :project_id
SELECT incident_id, component_id, severity, symptom_summary FROM incidents
WHERE status='open' AND project_id=:project_id ORDER BY incident_id;
