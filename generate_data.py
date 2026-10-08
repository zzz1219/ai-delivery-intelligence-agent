#!/usr/bin/env python3
"""
Rule-driven synthetic data generator for the AI Delivery Intelligence Agent.

All data is SYNTHETIC and anonymized. It is inspired by recurring patterns in
enterprise delivery work, not taken from any real customer, project or ticket.

Pipeline: business rules -> generator -> validator -> frozen dataset.

Planted patterns (each one serves exactly one benchmark question):
  spike cluster        -> H4  map_service incidents spike in Q1 2026 on v3.2 deployments
  recurrence cluster   -> H2  repeated crs_mismatch on Map Service within 90 days
  critical burst       -> S1  project P03 has a burst of critical incidents in Q3 2026
  open cluster         -> H5  P03 has the most open incidents; some have no documented case
Everything else follows the general rules (R1-R3, R5, R7).

What the validator checks:
  * temporal / referential integrity (incident time >= deployment time, incident is attached
    to the project's latest active deployment, open incidents expose no root cause)
  * planted patterns (ratios, spike, v3.2 share) and ranking margins for S1, H2, H5
  * that every case in evaluation_cases.json has non-empty ground truth
It does NOT run the agent; agent-side scoring is the evaluator's job (later step).

Usage:
  python generate_data.py                 # generate with the frozen seed; fail loudly on any violation
  python generate_data.py --search-seed   # development only: find the first seed that passes validation

Outputs (next to this script):
  data/delivery.db, data/csv/*.csv
  docs/historical_cases/INC_xxxx.md, docs/troubleshooting_guides/*.md, docs/deployment_guides/*.md
  ground_truth/reference_sql.sql, incidents_truth.csv, doc_manifest.csv,
               evaluation_cases.json, validation_report.txt
"""
import argparse
import csv
import json
import shutil
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from random import Random

OUT = Path(__file__).parent
FMT = "%Y-%m-%d %H:%M:%S"
FROZEN_SEED = 58

REF_DATE = datetime(2026, 10, 5)          # "today" for questions such as "last quarter" (= Q3 2026)
START, END = datetime(2025, 10, 1), datetime(2026, 9, 30, 23, 0)
N_INCIDENTS, N_DOCS = 200, 60
N_SPIKE, N_RECUR, N_CRIT, N_OPEN = 30, 9, 4, 8
TARGET_PROJECT = "P03"
V32_SHARE_MIN = 0.8                       # required before the answer may say "coincided with v3.2"

# --------------------------------------------------------------------------
# GENERAL RULES
#  R1  resolution_hours = component baseline x severity mult x env mult x noise
#  R2  critical x1.8 (multiplicative; realized aggregate ratio is lower, see report)
#  R3  distributed env: timeout incidents ~4x more likely per deployment
#  R4  components tied to specific root causes (ROOT_BY_LAYER)
#  R5  each root cause has 1-2 fixed solution patterns (SOLUTIONS)
#  R7  only 60 closed incidents have case docs; service_publish_failure has none
#  R8  repeat = same project + component + root_cause within 90 days (closed incidents)
#  R9  an incident is attached to its project's latest deployment at incident time
#  R10 open incidents expose no root_cause / solution_pattern (hidden truth kept separately)
# --------------------------------------------------------------------------

COMPONENTS = {
    "C01": ("Primary Database", "database"),
    "C02": ("Replica Database", "database"),
    "C03": ("Map Service", "map_service"),
    "C04": ("Tile Cache", "map_service"),
    "C05": ("Data Import Pipeline", "data_pipeline"),
    "C06": ("Web Frontend", "ui"),
    "C07": ("API Gateway", "network"),
    "C08": ("Auth Service", "auth"),
}
COMP_IDS = list(COMPONENTS)
COMP_WEIGHTS = [.15, .08, .17, .10, .15, .12, .13, .10]

ROOT_BY_LAYER = {
    "database": [("missing_index", .5), ("connection_pool_exhausted", .4), ("timeout", .1)],
    "map_service": [("tile_cache_misconfig", .35), ("crs_mismatch", .25),
                    ("service_publish_failure", .2), ("timeout", .2)],
    "data_pipeline": [("batch_size_too_large", .7), ("crs_mismatch", .15), ("timeout", .15)],
    "ui": [("frontend_config_error", .4), ("timeout", .4), ("authentication_failure", .2)],
    "network": [("timeout", .8), ("authentication_failure", .2)],
    "auth": [("authentication_failure", .9), ("timeout", .1)],
}
SOLUTIONS = {
    "missing_index": [("add_composite_index", .6), ("rebuild_spatial_index", .4)],
    "connection_pool_exhausted": [("increase_pool_size_and_timeouts", .7), ("fix_connection_leak", .3)],
    "timeout": [("increase_gateway_timeout_and_retry", .4), ("scale_out_service", .35),
                ("tune_query_timeouts", .25)],
    "tile_cache_misconfig": [("adjust_cache_scale_levels", .6), ("rebuild_tile_cache", .4)],
    "crs_mismatch": [("reproject_to_service_crs", .6), ("set_correct_srid", .4)],
    "service_publish_failure": [("republish_service_with_valid_config", .6), ("fix_service_permissions", .4)],
    "batch_size_too_large": [("reduce_batch_size_pause_index_rebuild", 1.0)],
    "authentication_failure": [("refresh_token_config", .5), ("fix_sso_mapping", .5)],
    "frontend_config_error": [("correct_frontend_config", 1.0)],
}
BASE_HOURS = {"database": 30, "map_service": 18, "data_pipeline": 14,
              "network": 12, "auth": 10, "ui": 8}
SEVERITY = [("critical", .15, 1.8), ("high", .25, 1.3), ("medium", .40, 1.0), ("low", .20, 0.7)]
SEV_MULT = {s: m for s, _, m in SEVERITY}
ENV_MULT = {"distributed": 1.2, "cloud": 1.0, "single_node": 1.0}
RECORD_ROOTS = ("missing_index", "batch_size_too_large")

CUSTOMERS = [
    ("CUS01", "Northbridge Municipal Bureau", "government", "North China"),
    ("CUS02", "Eastport Land Resources Agency", "government", "East China"),
    ("CUS03", "Greenvale Power Grid Co.", "energy", "Central China"),
    ("CUS04", "Harborline Transit Authority", "transport", "South China"),
    ("CUS05", "Clearwater Utilities Group", "utilities", "East China"),
    ("CUS06", "Summit Natural Resources Dept.", "government", "Southwest China"),
    ("CUS07", "Riverside Urban Planning Institute", "government", "East China"),
    ("CUS08", "Ironwood Mining Corp.", "energy", "North China"),
    ("CUS09", "Baywatch Port Logistics", "transport", "South China"),
    ("CUS10", "Lakeshore Water Services", "utilities", "Central China"),
]
PROJECT_NAMES = ["Urban Spatial Data Platform", "Land Survey Management System", "Grid Asset Mapping",
                 "Transit Network Digital Twin", "Pipeline Monitoring Portal", "Resource Census Platform",
                 "Planning Review System", "Mine Safety Mapping", "Port Operations Map",
                 "Water Network Platform"]
DEPLOY_SCHEDULE = [("v3.0", "2025-06-12"), ("v3.0", "2025-07-08"), ("v3.0", "2025-08-20"),
                   ("v3.0", "2025-09-15"), ("v3.1", "2025-10-10"), ("v3.1", "2025-11-05"),
                   ("v3.1", "2025-11-28"), ("v3.1", "2025-12-12"), ("v3.2", "2026-01-06"),
                   ("v3.2", "2026-01-09"), ("v3.2", "2026-01-12"), ("v3.2", "2026-01-14"),
                   ("v3.3", "2026-05-18"), ("v3.3", "2026-06-22"), ("v3.3", "2026-07-20")]
DEPLOY_ENVS = ["single_node", "cloud", "distributed", "single_node", "cloud", "single_node",
               "distributed", "cloud", "distributed", "single_node", "cloud", "distributed",
               "single_node", "cloud", "single_node"]

SCHEMA = """
CREATE TABLE customers(customer_id TEXT PRIMARY KEY, name TEXT, industry TEXT, region TEXT);
CREATE TABLE projects(project_id TEXT PRIMARY KEY, customer_id TEXT REFERENCES customers(customer_id),
  name TEXT, start_date TEXT, status TEXT, sla_tier TEXT);
CREATE TABLE components(component_id TEXT PRIMARY KEY, name TEXT, layer TEXT);
CREATE TABLE deployments(deployment_id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(project_id),
  env_type TEXT, version TEXT, deployed_at TEXT);
CREATE TABLE incidents(incident_id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(project_id),
  deployment_id TEXT REFERENCES deployments(deployment_id), component_id TEXT REFERENCES components(component_id),
  severity TEXT, category TEXT, reported_at TEXT, resolved_at TEXT, status TEXT,
  root_cause TEXT, solution_pattern TEXT, resolution_summary TEXT, symptom_summary TEXT);
"""
COLUMNS = {
    "customers": ["customer_id", "name", "industry", "region"],
    "projects": ["project_id", "customer_id", "name", "start_date", "status", "sla_tier"],
    "components": ["component_id", "name", "layer"],
    "deployments": ["deployment_id", "project_id", "env_type", "version", "deployed_at"],
    "incidents": ["incident_id", "project_id", "deployment_id", "component_id", "severity", "category",
                  "reported_at", "resolved_at", "status", "root_cause", "solution_pattern",
                  "resolution_summary", "symptom_summary"],
}
HIDDEN_WHEN_OPEN = {"root_cause", "solution_pattern", "resolution_summary"}

REFERENCE_SQL = {
    "S1_most_critical_incidents_last_quarter": """
-- reference date 2026-10-05 => last quarter = 2026-Q3
SELECT project_id, COUNT(*) AS critical_incidents FROM incidents
WHERE severity='critical' AND reported_at >= '2026-07-01' AND reported_at < '2026-10-01'
GROUP BY project_id ORDER BY critical_incidents DESC;""",
    "S2_avg_resolution_hours_by_category": """
SELECT category, ROUND(AVG((julianday(resolved_at)-julianday(reported_at))*24),1) AS avg_hours
FROM incidents WHERE status='closed' GROUP BY category ORDER BY avg_hours DESC;""",
    "S3_map_service_monthly_trend": """
SELECT strftime('%Y-%m', reported_at) AS month, COUNT(*) AS incidents FROM incidents
WHERE category='map_service' GROUP BY month ORDER BY month;""",
    "S4_timeouts_per_deployment_by_env": """
-- observed timeout incidents per deployment (root_cause is known only for closed incidents);
-- this is NOT a failure rate: deployments have different exposure periods.
SELECT d.env_type,
  ROUND(1.0*SUM(CASE WHEN i.root_cause='timeout' THEN 1 ELSE 0 END)/COUNT(DISTINCT d.deployment_id),2)
    AS timeout_incidents_per_deployment
FROM deployments d LEFT JOIN incidents i ON i.deployment_id=d.deployment_id
GROUP BY d.env_type ORDER BY 2 DESC;""",
    "S5_open_share_by_severity": """
SELECT severity, ROUND(100.0*SUM(status='open')/COUNT(*),1) AS pct_open FROM incidents GROUP BY severity;""",
    "H1_resolution_hours_database_vs_others": """
SELECT CASE WHEN category='database' THEN 'database' ELSE 'other' END AS grp,
  ROUND(AVG((julianday(resolved_at)-julianday(reported_at))*24),1) AS avg_hours
FROM incidents WHERE status='closed' GROUP BY grp;""",
    "H2_repeat_incidents_by_component": """
-- repeat = same project_id + component_id + root_cause within 90 days (closed incidents only,
-- because root_cause is NULL for open incidents)
WITH repeats AS (
  SELECT a.incident_id AS id, a.component_id FROM incidents a JOIN incidents b
    ON a.project_id=b.project_id AND a.component_id=b.component_id
   AND a.root_cause=b.root_cause AND a.incident_id<>b.incident_id
   AND ABS(julianday(a.reported_at)-julianday(b.reported_at))<=90)
SELECT component_id, COUNT(DISTINCT id) AS repeat_incidents FROM repeats
GROUP BY component_id ORDER BY repeat_incidents DESC;""",
    "H2b_repeat_incident_details": """
-- parameter: :component_id
WITH repeats AS (
  SELECT DISTINCT a.incident_id AS id FROM incidents a JOIN incidents b
    ON a.project_id=b.project_id AND a.component_id=b.component_id
   AND a.root_cause=b.root_cause AND a.incident_id<>b.incident_id
   AND ABS(julianday(a.reported_at)-julianday(b.reported_at))<=90
  WHERE a.component_id=:component_id)
SELECT i.incident_id, i.project_id, i.root_cause, i.solution_pattern FROM incidents i
JOIN repeats r ON r.id=i.incident_id ORDER BY i.incident_id;""",
    "H3_critical_in_distributed": """
SELECT ROUND(AVG((julianday(i.resolved_at)-julianday(i.reported_at))*24),1) AS avg_hours,
  COUNT(*) AS n FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.severity='critical' AND d.env_type='distributed' AND i.status='closed';""",
    "H3b_fix_counts_critical_distributed": """
SELECT i.solution_pattern, COUNT(*) AS n FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.severity='critical' AND d.env_type='distributed' AND i.status='closed'
GROUP BY i.solution_pattern ORDER BY n DESC;""",
    "H4_map_service_by_quarter": """
SELECT strftime('%Y',reported_at)||'-Q'||((CAST(strftime('%m',reported_at) AS INTEGER)+2)/3) AS quarter,
  COUNT(*) AS incidents FROM incidents WHERE category='map_service' GROUP BY quarter ORDER BY quarter;""",
    "H4b_q1_map_service_by_version": """
-- the spike may be described as "coincided with v3.2" only if v3.2 holds >= 80% of these incidents
SELECT d.version, COUNT(*) AS incidents FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.category='map_service' AND i.reported_at >= '2026-01-01' AND i.reported_at < '2026-04-01'
GROUP BY d.version ORDER BY incidents DESC;""",
    "H5_open_incidents_by_project": """
SELECT project_id, COUNT(*) AS open_incidents FROM incidents WHERE status='open'
GROUP BY project_id ORDER BY open_incidents DESC;""",
    "H5b_open_incidents_of_project": """
-- parameter: :project_id
SELECT incident_id, component_id, severity, symptom_summary FROM incidents
WHERE status='open' AND project_id=:project_id ORDER BY incident_id;""",
}


# ----------------------------------------------------------------- generation
def pick(rng, pairs):
    items, weights = zip(*pairs)
    return rng.choices(items, weights=weights)[0]


def rand_dt(rng, lo, hi):
    dt = lo + timedelta(seconds=rng.uniform(0, (hi - lo).total_seconds()))
    return dt.replace(hour=rng.randint(8, 18), minute=rng.randint(0, 59), second=0, microsecond=0)


def build_static():
    deployments = [dict(deployment_id=f"D{i+1:02d}", project_id=f"P{(i % 10)+1:02d}", env_type=env,
                        version=ver, deployed_at=datetime.fromisoformat(dt))
                   for i, ((ver, dt), env) in enumerate(zip(DEPLOY_SCHEDULE, DEPLOY_ENVS))]
    first = {}
    for d in deployments:
        first[d["project_id"]] = min(first.get(d["project_id"], d["deployed_at"]), d["deployed_at"])
    tiers = ["gold", "silver", "bronze"]
    projects = [dict(project_id=f"P{i+1:02d}", customer_id=CUSTOMERS[i][0], name=PROJECT_NAMES[i],
                     start_date=(first[f"P{i+1:02d}"] - timedelta(days=60)).strftime("%Y-%m-%d"),
                     status="active", sla_tier=tiers[i % 3]) for i in range(10)]
    customers = [dict(zip(COLUMNS["customers"], c)) for c in CUSTOMERS]
    components = [dict(component_id=k, name=v[0], layer=v[1]) for k, v in COMPONENTS.items()]
    return customers, projects, components, deployments


def active_deployments(deps, dt, project=None):
    """R9: for each project keep only the latest deployment already live at time dt."""
    latest = {}
    for d in sorted(deps, key=lambda d: d["deployed_at"]):
        if d["deployed_at"] <= dt and (project is None or d["project_id"] == project):
            latest[d["project_id"]] = d
    return list(latest.values())


# symptom_summary as stored in the database (wording differs from the case-doc text on purpose)
SYMPTOM_DB = {
    "missing_index": ["Severe latency after a bulk import of approximately {records}M records; map queries slow or timing out.",
                      "Queries on large tables became very slow following a data load of about {records}M records."],
    "connection_pool_exhausted": ["Intermittent connection errors and rising response time during peak hours; recovers after restart.",
                                  "Service becomes unresponsive under concurrent load with no free database connections."],
    "timeout": ["Requests fail with timeout (504) errors, especially at peak usage.",
                "Users wait a long time and then receive timeout errors."],
    "tile_cache_misconfig": ["Map display is slow with blank or stale tiles at some zoom levels.",
                             "Map latency increased and tiles keep being regenerated instead of served from cache."],
    "crs_mismatch": ["Newly loaded layers display offset from the base map or spatial joins return nothing.",
                     "Data loads fine but features appear in the wrong place."],
    "service_publish_failure": ["Map service cannot be published or starts in an error state after a configuration change.",
                                "Publishing the map service fails and the service stays unavailable."],
    "batch_size_too_large": ["Bulk import of about {records}M records stalls or fails with memory or lock errors.",
                             "Large single-batch import of {records}M records makes the system very slow."],
    "authentication_failure": ["Users are bounced back to the login page or receive 401 errors after an authentication change.",
                               "Token validation fails intermittently and blocks several modules."],
    "frontend_config_error": ["Web pages show blank panels or fail to load after a configuration update.",
                              "Frontend requests go to the wrong endpoint and panels stay empty."],
}
ROOT_DESC = {
    "missing_index": "Slow queries after a large data load were traced to a missing index.",
    "connection_pool_exhausted": "Intermittent failures under load were traced to connection pool exhaustion.",
    "timeout": "Intermittent request failures were traced to mismatched or too-short timeouts.",
    "tile_cache_misconfig": "Slow or stale map tiles were traced to a tile cache misconfiguration.",
    "crs_mismatch": "Misplaced or unjoinable features were traced to a coordinate system mismatch.",
    "service_publish_failure": "A map service failed to publish.",
    "batch_size_too_large": "A bulk import stalled because the batch size was too large.",
    "authentication_failure": "Login or token failures were traced to authentication configuration.",
    "frontend_config_error": "Blank or failing pages were traced to a frontend configuration error.",
}
FIX_TEXT = {
    "add_composite_index": "Added a composite index on the frequent filter columns and refreshed table statistics.",
    "rebuild_spatial_index": "Rebuilt the spatial index and re-analyzed the affected tables.",
    "increase_pool_size_and_timeouts": "Increased the connection pool size and tuned idle and acquisition timeouts.",
    "fix_connection_leak": "Patched the leaking data-access path and restarted dependent services; pool usage stabilized.",
    "tune_query_timeouts": "Aligned query and gateway timeouts and optimized the slowest queries.",
    "increase_gateway_timeout_and_retry": "Raised gateway timeouts and added bounded retry with backoff.",
    "scale_out_service": "Added service instances behind the load balancer and rebalanced traffic.",
    "rebuild_tile_cache": "Rebuilt the tile cache for the affected scale levels.",
    "adjust_cache_scale_levels": "Restored the cache scale-level configuration and re-seeded tiles.",
    "reproject_to_service_crs": "Reprojected the loaded layers to the service coordinate system.",
    "set_correct_srid": "Corrected the SRID metadata of the source data and reloaded it.",
    "republish_service_with_valid_config": "Republished the service with corrected configuration.",
    "fix_service_permissions": "Fixed the service account permissions and republished.",
    "reduce_batch_size_pause_index_rebuild": "Reduced the import batch size and paused index rebuilding until the load finished.",
    "refresh_token_config": "Corrected token validation settings and refreshed signing keys.",
    "fix_sso_mapping": "Fixed the SSO attribute mapping.",
    "correct_frontend_config": "Corrected the frontend endpoint configuration.",
}


def gen_incident(rng, deps, comp=None, root=None, dt=None, project=None, version=None,
                 severity=None, force_open=False, tag=None):
    comp = comp or rng.choices(COMP_IDS, weights=COMP_WEIGHTS)[0]
    layer = COMPONENTS[comp][1]
    root = root or pick(rng, ROOT_BY_LAYER[layer])
    dt = dt or rand_dt(rng, START, END)
    cands = active_deployments(deps, dt, project)
    if version:
        cands = [d for d in cands if d["version"] == version]
    weights = [4.0 if (root == "timeout" and d["env_type"] == "distributed") else 1.0 for d in cands]  # R3
    dep = rng.choices(cands, weights=weights)[0]
    sev = severity or pick(rng, [(s, p) for s, p, _ in SEVERITY])
    noise = rng.lognormvariate(-0.03125, 0.25)
    hours = max(0.5, round(BASE_HOURS[layer] * SEV_MULT[sev] * ENV_MULT[dep["env_type"]] * noise, 1))
    records = rng.choice([2, 5, 10, 15, 20]) if root in RECORD_ROOTS else None
    return dict(project_id=dep["project_id"], deployment_id=dep["deployment_id"], component_id=comp,
                severity=sev, category=layer, reported_at=dt, hours=hours, root_cause=root,
                version=dep["version"], env_type=dep["env_type"], records_affected=records,
                symptom_summary=rng.choice(SYMPTOM_DB[root]).format(records=records),
                force_open=force_open, tag=tag, has_doc=False)


def generate(seed):
    rng = Random(seed)
    customers, projects, components, deps = build_static()
    incs = []
    n_base = N_INCIDENTS - N_SPIKE - N_RECUR - N_CRIT - N_OPEN
    for _ in range(n_base):
        incs.append(gen_incident(rng, deps))
    for _ in range(N_SPIKE):                                         # H4 spike cluster
        incs.append(gen_incident(rng, deps, comp=rng.choice(["C03", "C04"]),
                                 root=pick(rng, [("tile_cache_misconfig", .7), ("timeout", .3)]),
                                 dt=rand_dt(rng, datetime(2026, 1, 16), datetime(2026, 3, 31)),
                                 version="v3.2", tag="spike"))
    for _ in range(N_RECUR):                                         # H2 recurrence cluster
        incs.append(gen_incident(rng, deps, comp="C03", root="crs_mismatch", project="P06",
                                 dt=rand_dt(rng, datetime(2026, 2, 1), datetime(2026, 4, 5)), tag="recurrence"))
    for _ in range(N_CRIT):                                          # S1 critical burst
        incs.append(gen_incident(rng, deps, project=TARGET_PROJECT, severity="critical",
                                 dt=rand_dt(rng, datetime(2026, 7, 1), datetime(2026, 9, 30)), tag="crit_burst"))
    open_specs = [("C03", "service_publish_failure"), ("C03", "service_publish_failure"),   # H5 open cluster
                  ("C04", "tile_cache_misconfig"), ("C01", "missing_index"), ("C07", "timeout"),
                  ("C05", "batch_size_too_large"), ("C03", "crs_mismatch"), ("C01", "connection_pool_exhausted")]
    assert len(open_specs) == N_OPEN
    for comp, root in open_specs:
        incs.append(gen_incident(rng, deps, comp=comp, root=root, project=TARGET_PROJECT, force_open=True,
                                 dt=rand_dt(rng, datetime(2026, 8, 15), datetime(2026, 9, 30)), tag="open_cluster"))

    incs.sort(key=lambda i: i["reported_at"])
    for n, inc in enumerate(incs, 1):
        inc["incident_id"] = f"INC_{n:04d}"
        inc["status"] = "open" if inc["force_open"] else "closed"
        inc["resolved_at"] = inc["reported_at"] + timedelta(hours=inc["hours"])
    # background open incidents: recent ones plus a few stale ones (general incidents only)
    plain = [i for i in incs if i["tag"] is None]
    for inc in plain:
        if inc["reported_at"] >= datetime(2026, 8, 15) and rng.random() < 0.35:
            inc["status"] = "open"
    mid = [i for i in plain if datetime(2026, 6, 1) <= i["reported_at"] < datetime(2026, 8, 15)
           and i["status"] == "closed"]
    for inc in rng.sample(mid, 3):
        inc["status"] = "open"
    for inc in incs:
        if inc["status"] == "closed" and inc["resolved_at"] > REF_DATE:
            inc["status"] = "open"
    for inc in incs:
        inc["solution_pattern"] = pick(rng, SOLUTIONS[inc["root_cause"]])   # hidden while open (R10)
        if inc["status"] == "open":
            inc["resolved_at"] = None
            inc["resolution_summary"] = None
        else:
            inc["resolution_summary"] = f"{ROOT_DESC[inc['root_cause']]} {FIX_TEXT[inc['solution_pattern']]}"

    # R7: choose docs among closed incidents (never service_publish_failure)
    closed = [i for i in incs if i["status"] == "closed" and i["root_cause"] != "service_publish_failure"]
    chosen, used = [], set()

    def add(i):
        if i["incident_id"] not in used and len(chosen) < N_DOCS:
            used.add(i["incident_id"])
            i["has_doc"] = True
            chosen.append(i)

    for i in [x for x in closed if x["version"] == "v3.2" and x["root_cause"] == "tile_cache_misconfig"][:6]:
        add(i)
    for i in [x for x in closed if x["tag"] == "recurrence"][:3]:
        add(i)
    for i in [x for x in closed if x["root_cause"] in RECORD_ROOTS and x["records_affected"] >= 10][:4]:
        add(i)
    for i in [x for x in closed if x["root_cause"] == "timeout" and x["env_type"] == "distributed"][:3]:
        add(i)
    by_pat = defaultdict(list)
    for i in closed:
        by_pat[i["solution_pattern"]].append(i)
    for _, lst in sorted(by_pat.items()):
        for i in lst[:2]:
            add(i)
    rest = [i for i in closed if i["incident_id"] not in used]
    rng.shuffle(rest)
    for i in rest:
        add(i)
    return dict(customers=customers, projects=projects, components=components, deployments=deps,
                incidents=incs, docs=sorted(chosen, key=lambda i: i["incident_id"]))


# ----------------------------------------------------------------- database
def fmt(v):
    return v.strftime(FMT) if isinstance(v, datetime) else v


def cell(table, row, col):
    """Public value of a column; open incidents never expose root cause / solution (R10)."""
    if table == "incidents" and row["status"] == "open" and col in HIDDEN_WHEN_OPEN:
        return None
    return fmt(row[col])


def build_db(conn, data):
    conn.executescript(SCHEMA)
    for table, cols in COLUMNS.items():
        rows = [[cell(table, r, c) for c in cols] for r in data[table]]
        conn.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(cols))})", rows)
    conn.commit()


def rows_of(conn, name, params=None):
    return conn.execute(REFERENCE_SQL[name], params or {}).fetchall()


# ----------------------------------------------------------------- evaluation cases
BENCHMARK_VERSION = "1.1"
BENCHMARK_CHANGELOG = [
    "S4 wording now asks for every environment type and fixes the denominator (all deployments of the type); "
    "v1.0 left the denominator ambiguous and let a top-1 answer hide a wrong denominator.",
    "S5 wording now says 'percentage of that severity's incidents' (v1.0 could also be read as the composition of "
    "open incidents).",
    "Each SQL case carries a scoring 'contract' (kind, key column, value-column hints, tolerance, accepted scales) "
    "so the evaluator scores what the question asks for, not the question id.",
    "Data, reference SQL and gold values are unchanged; the agent and its prompts are unchanged.",
]
# scoring contracts: how a result is judged for each SQL question
SQL_CONTRACTS = {
    "S1": dict(kind="top1", entity="project_id", value_hints=["critical"], tolerance=0.0),
    "S2": dict(kind="table", key="category", value_hints=["avg", "average", "mean"],
               tolerance=0.15),
    "S3": dict(kind="table", key="month", value_hints=["count", "incident", "number"], tolerance=0.0),
    "S4": dict(kind="table", key="env_type", loose_rank=True, tolerance=0.15,
               value_hints=["per_deployment", "rate", "ratio", "avg", "average"]),
    "S5": dict(kind="table", key="severity", tolerance=0.15, gold_scale="percent",
               metric=dict(type="proportion", accepted_scales=["fraction", "percent"]),
               value_hints=["share", "pct", "percent", "rate", "ratio", "proportion", "fraction"]),
}


def build_eval_cases(data, conn):
    """Machine-readable benchmark; ground truth is computed from the data, never hand-written."""
    incs = {i["incident_id"]: i for i in data["incidents"]}
    docs = data["docs"]
    problems = []

    def dids(pred):
        return sorted(d["incident_id"] for d in docs if pred(d))

    def need(cond, msg):
        if not cond:
            problems.append(msg)

    cases = []
    s1 = rows_of(conn, "S1_most_critical_incidents_last_quarter")
    cases.append(dict(id="S1", type="sql", expected_route="SQL",
                      question="Which project had the most critical incidents last quarter?",
                      reference_sql=["S1_most_critical_incidents_last_quarter"],
                      gold=dict(answer=s1[0][0], ranking=s1[:4]), expected_doc_ids=[], expected_guides=[]))
    for cid, name, question in [
            ("S2", "S2_avg_resolution_hours_by_category", "What is the average resolution time by category?"),
            ("S3", "S3_map_service_monthly_trend", "Show the monthly incident trend for the map_service component."),
            ("S4", "S4_timeouts_per_deployment_by_env", "For each environment type, report the average number of timeout incidents per deployment, "
             "using all deployments of that environment type as the denominator, and identify which "
             "environment type has the highest rate."),
            ("S5", "S5_open_share_by_severity", "For each severity level, what percentage of that severity's incidents are still open?")]:
        cases.append(dict(id=cid, type="sql", expected_route="SQL", question=question, reference_sql=[name],
                          gold=dict(rows=rows_of(conn, name)), expected_doc_ids=[], expected_guides=[]))
    for c in cases:                       # attach scoring contracts to the SQL cases
        if c["id"] in SQL_CONTRACTS:
            c["contract"] = SQL_CONTRACTS[c["id"]]
            if c["id"] == "S1":
                c["gold"]["top_value"] = s1[0][1]

    r1 = dids(lambda d: d["root_cause"] in RECORD_ROOTS and d["records_affected"] >= 10)
    need(len(r1) >= 2, "R1 needs >=2 gold docs")
    cases.append(dict(id="R1", type="rag", expected_route="RAG",
                      question="A customer reports severe latency after importing 10 million records. What should we check first?",
                      reference_sql=[], gold=dict(first_checks=["import batch size / transaction length",
                                                               "index existence and statistics after the load"]),
                      expected_doc_ids=r1,
                      expected_guides=["bulk_import_performance.md", "slow_queries_missing_index.md"]))
    r2 = dids(lambda d: d["root_cause"] == "crs_mismatch")
    need(len(r2) >= 2, "R2 needs >=2 gold docs")
    cases.append(dict(id="R2", type="rag", expected_route="RAG",
                      question="What are the usual fixes for coordinate-system mismatch when loading data?",
                      reference_sql=[], gold=dict(fixes=["reproject_to_service_crs", "set_correct_srid"]),
                      expected_doc_ids=r2, expected_guides=["crs_mismatch.md"]))
    r3 = dids(lambda d: d["root_cause"] == "timeout" and d["env_type"] == "distributed")
    need(len(r3) >= 2, "R3 needs >=2 gold docs")
    cases.append(dict(id="R3", type="rag", expected_route="RAG",
                      question="Have we seen service timeouts after distributed deployments? What was found?",
                      reference_sql=[], gold=dict(), expected_doc_ids=r3, expected_guides=["service_timeouts.md"]))
    r4 = dids(lambda d: d["root_cause"] == "connection_pool_exhausted")
    need(len(r4) >= 1, "R4 needs >=1 gold doc")
    cases.append(dict(id="R4", type="rag", expected_route="RAG",
                      question="What does the troubleshooting guide say about connection pool exhaustion?",
                      reference_sql=[], gold=dict(), expected_doc_ids=r4,
                      expected_guides=["connection_pool_exhausted.md"]))
    cases.append(dict(id="R5", type="rag", expected_route="RAG",
                      question="What should be checked before go-live of a distributed deployment?",
                      reference_sql=[], gold=dict(), expected_doc_ids=[],
                      expected_guides=["distributed_deployment.md"]))

    h1 = rows_of(conn, "H1_resolution_hours_database_vs_others")
    db_fix = Counter(i["solution_pattern"] for i in incs.values()
                     if i["status"] == "closed" and i["category"] == "database")
    cases.append(dict(id="H1", type="hybrid", expected_route="HYBRID",
                      question="Are database incidents slower to resolve than others, and which fixes worked before?",
                      reference_sql=["H1_resolution_hours_database_vs_others"],
                      gold=dict(avg_hours=dict(h1), database_slower=dict(h1)["database"] > dict(h1)["other"],
                                top_fixes=db_fix.most_common(3)),
                      expected_doc_ids=dids(lambda d: d["category"] == "database"), expected_guides=[]))

    rep = rows_of(conn, "H2_repeat_incidents_by_component")
    top_comp = rep[0][0]
    det = rows_of(conn, "H2b_repeat_incident_details", {"component_id": top_comp})
    h2_root = Counter(r[2] for r in det)
    h2_fix = Counter(r[3] for r in det)
    cases.append(dict(id="H2", type="hybrid", expected_route="HYBRID",
                      question="Which component has the most repeat incidents, and what root causes and solutions appear?",
                      reference_sql=["H2_repeat_incidents_by_component", "H2b_repeat_incident_details"],
                      gold=dict(top_component=top_comp, ranking=rep[:4], root_causes=h2_root.most_common(),
                                solutions=h2_fix.most_common()),
                      expected_doc_ids=dids(lambda d: d["incident_id"] in {r[0] for r in det}),
                      expected_guides=[]))

    h3 = rows_of(conn, "H3_critical_in_distributed")
    h3b = rows_of(conn, "H3b_fix_counts_critical_distributed")
    need(len(h3b) >= 1 and (len(h3b) == 1 or h3b[0][1] > h3b[1][1]), "H3 most common fix not unique")
    cases.append(dict(id="H3", type="hybrid", expected_route="HYBRID",
                      question="How long do critical incidents take in distributed deployments, and what fix was most common?",
                      reference_sql=["H3_critical_in_distributed", "H3b_fix_counts_critical_distributed"],
                      gold=dict(avg_hours=h3[0][0], n=h3[0][1], fix_counts=h3b,
                                most_common_fix=h3b[0][0] if h3b else None),
                      expected_doc_ids=dids(lambda d: d["severity"] == "critical" and d["env_type"] == "distributed"),
                      expected_guides=[]))

    qtr = dict(rows_of(conn, "H4_map_service_by_quarter"))
    ver = dict(rows_of(conn, "H4b_q1_map_service_by_version"))
    share = ver.get("v3.2", 0) / sum(ver.values())
    q1_docs = dids(lambda d: d["category"] == "map_service"
                   and datetime(2026, 1, 1) <= d["reported_at"] < datetime(2026, 4, 1))
    need(share >= V32_SHARE_MIN, f"H4 v3.2 share {share:.2f} < {V32_SHARE_MIN}")
    need(len(q1_docs) >= 3, "H4 needs >=3 gold docs")
    cases.append(dict(id="H4", type="hybrid", expected_route="HYBRID",
                      question="Did map_service incidents spike in Q1 2026, and what causes were documented?",
                      reference_sql=["H4_map_service_by_quarter", "H4b_q1_map_service_by_version"],
                      gold=dict(by_quarter=qtr, q1_by_version=ver, v32_share=round(share, 2),
                                spike=True, may_say_coincided_with_v3_2=share >= V32_SHARE_MIN,
                                documented_causes=Counter(incs[i]["root_cause"] for i in q1_docs).most_common()),
                      expected_doc_ids=q1_docs, expected_guides=["tile_cache_issues.md"]))

    op = rows_of(conn, "H5_open_incidents_by_project")
    top_proj = op[0][0]
    open_rows = rows_of(conn, "H5b_open_incidents_of_project", {"project_id": top_proj})
    undoc = [r[0] for r in open_rows if incs[r[0]]["root_cause"] == "service_publish_failure"]
    similar = {r[0]: dids(lambda d, rc=incs[r[0]]["root_cause"]: d["root_cause"] == rc)
               for r in open_rows if incs[r[0]]["root_cause"] != "service_publish_failure"}
    similar = {k: v for k, v in similar.items() if v}
    need(len(undoc) >= 2, "H5 needs >=2 open incidents without any documented case")
    need(len(similar) >= 2, "H5 needs >=2 open incidents with documented similar cases")
    cases.append(dict(id="H5", type="hybrid", expected_route="HYBRID",
                      question="For the project with the most open incidents, are there similar closed cases?",
                      reference_sql=["H5_open_incidents_by_project", "H5b_open_incidents_of_project"],
                      gold=dict(project=top_proj, open_incident_ids=[r[0] for r in open_rows],
                                undocumented_incident_ids=undoc,
                                expected_behavior_for_undocumented="state that no directly documented closed case was found",
                                similar_doc_ids_by_open_incident=similar),
                      expected_doc_ids=sorted({d for v in similar.values() for d in v}), expected_guides=[]))
    return cases, problems


# ----------------------------------------------------------------- validation
def validate(data):
    conn = sqlite3.connect(":memory:")
    build_db(conn, data)
    fails, notes = [], {}

    def one(sql):
        return conn.execute(sql).fetchone()[0]

    # integrity
    if one("""SELECT COUNT(*) FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
              WHERE i.reported_at < d.deployed_at OR d.project_id <> i.project_id"""):
        fails.append("integrity: incident before its deployment or project mismatch")
    stale = one("""SELECT COUNT(*) FROM incidents i WHERE i.deployment_id <> (SELECT d.deployment_id FROM deployments d
              WHERE d.project_id=i.project_id AND d.deployed_at<=i.reported_at ORDER BY d.deployed_at DESC LIMIT 1)""")
    notes["incidents_not_on_latest_active_deployment"] = stale
    if stale:
        fails.append("R9: incidents attached to a superseded deployment")
    if one("""SELECT COUNT(*) FROM deployments d JOIN projects p ON p.project_id=d.project_id
              WHERE d.deployed_at < p.start_date"""):
        fails.append("integrity: deployment before project start")
    if one("""SELECT COUNT(*) FROM incidents WHERE (status='closed' AND resolved_at < reported_at)
              OR (status='open' AND (resolved_at IS NOT NULL OR root_cause IS NOT NULL OR solution_pattern IS NOT NULL))"""):
        fails.append("R10: open/closed field inconsistency")

    # general rules
    cat = dict(rows_of(conn, "S2_avg_resolution_hours_by_category"))
    notes["avg_hours_by_category"] = cat
    if cat["database"] / cat["ui"] < 3:
        fails.append("R1 database/ui ratio < 3")
    crit = conn.execute("""SELECT AVG(CASE WHEN severity='critical' THEN h END), AVG(CASE WHEN severity<>'critical' THEN h END)
        FROM (SELECT severity,(julianday(resolved_at)-julianday(reported_at))*24 h FROM incidents WHERE status='closed')""").fetchone()
    notes["critical_vs_other_ratio (parameter 1.8)"] = round(crit[0] / crit[1], 2)
    if not 1.4 <= crit[0] / crit[1] <= 2.1:
        fails.append("R2 critical ratio outside 1.4-2.1")
    env = rows_of(conn, "S4_timeouts_per_deployment_by_env")
    notes["timeouts_per_deployment"] = env
    if env[0][0] != "distributed" or env[0][1] < 3 * env[1][1]:
        fails.append("R3 distributed timeout density < 3x next env")

    # planted patterns / ranking margins
    qtr = dict(rows_of(conn, "H4_map_service_by_quarter"))
    notes["map_service_by_quarter"] = qtr
    if qtr.get("2026-Q1", 0) < 2 * max(v for k, v in qtr.items() if k != "2026-Q1"):
        fails.append("H4 Q1 spike not >= 2x any other quarter")
    s1 = rows_of(conn, "S1_most_critical_incidents_last_quarter")
    notes["critical_q3_by_project"] = s1[:4]
    if len(s1) < 2 or s1[0][1] - s1[1][1] < 2:
        fails.append("S1 margin < 2")
    rep = rows_of(conn, "H2_repeat_incidents_by_component")
    notes["repeat_incidents_by_component"] = rep[:4]
    if len(rep) < 2 or rep[0][1] < 1.25 * rep[1][1]:
        fails.append("H2 top component < 1.25x second")
    op = rows_of(conn, "H5_open_incidents_by_project")
    notes["open_by_project"] = op
    if len(op) < 2 or op[0][1] - op[1][1] < 2:
        fails.append("H5 open-incident margin < 2")
    if len(data["docs"]) != N_DOCS:
        fails.append("doc count != 60")
    if len({d["root_cause"] for d in data["docs"]}) < 7:
        fails.append("docs cover fewer than 7 root causes")

    cases, problems = build_eval_cases(data, conn)
    fails += problems
    for c in cases:
        if c["type"] == "sql" and not c["gold"]:
            fails.append(f"{c['id']} has empty gold")
    return fails, notes, cases


# ----------------------------------------------------------------- documents
SYMPTOMS = {
    "missing_index": ["Queries on the {comp} slowed sharply after roughly {records} million records were loaded; map refresh took over 30 seconds.",
                      "Spatial queries began timing out for several users after a bulk data load of about {records} million records."],
    "connection_pool_exhausted": ["Users saw intermittent 'cannot acquire connection' errors during peak hours; response time climbed until restart.",
                                  "Services became unresponsive under concurrent load and the {comp} reported no free connections."],
    "timeout": ["Requests through the {comp} intermittently failed with 504 timeouts, mostly at peak usage.",
                "Users reported long waits followed by timeout errors in the {env} environment."],
    "tile_cache_misconfig": ["Map tiles loaded slowly and some zoom levels rendered blank or stale.",
                             "The cache hit rate fell sharply and tiles at large scales were regenerated on every request."],
    "crs_mismatch": ["Newly loaded layers appeared offset from the base map by hundreds of meters.",
                     "The data load completed but features displayed in the wrong location or failed spatial joins."],
    "batch_size_too_large": ["A bulk import of about {records} million records stalled and then failed with memory or lock-wait errors.",
                             "System response time degraded severely during and after importing {records} million records in a single batch."],
    "authentication_failure": ["Users were redirected back to the login page or received 401 errors after an SSO change.",
                               "Token validation failed intermittently and blocked access to several modules."],
    "frontend_config_error": ["The web frontend showed blank panels because of an incorrect endpoint setting.",
                              "Some pages failed to load after configuration was updated for the {env} environment."],
}
INVESTIGATION = {
    "missing_index": "Execution plans showed sequential scans on the largest tables; index statistics confirmed no usable composite or spatial index for the new data volume.",
    "connection_pool_exhausted": "Pool metrics showed active connections pinned at the maximum while idle time was near zero; some connections were never returned.",
    "timeout": "Gateway and backend logs showed requests exceeding the configured timeout while upstream services were still processing.",
    "tile_cache_misconfig": "Cache statistics showed a very low hit rate; scale-level settings did not match the published service.",
    "crs_mismatch": "Comparing layer metadata against the service showed different spatial reference identifiers.",
    "batch_size_too_large": "Import logs showed one oversized transaction holding locks while indexes were rebuilt after every batch.",
    "authentication_failure": "Auth service logs showed token signature or attribute-mapping errors correlated with the reported failures.",
    "frontend_config_error": "Browser network traces showed requests going to an outdated endpoint.",
}
LESSON = {
    "missing_index": "Check index coverage before and after any large data load.",
    "connection_pool_exhausted": "Size pools for peak concurrency and alert on pool saturation.",
    "timeout": "Keep timeouts consistent across gateway, services and database.",
    "tile_cache_misconfig": "Verify cache scale levels after every upgrade or service republish.",
    "crs_mismatch": "Validate the spatial reference of incoming data before loading.",
    "batch_size_too_large": "Load large datasets in bounded batches and defer index rebuilds.",
    "authentication_failure": "Test SSO and token settings in staging before changes go live.",
    "frontend_config_error": "Keep environment-specific configuration under version control.",
}


def render_case(inc, T, rng):
    comp_name = COMPONENTS[inc["component_id"]][0]
    proj = next(p for p in T["projects"] if p["project_id"] == inc["project_id"])
    industry = next(c for c in T["customers"] if c["customer_id"] == proj["customer_id"])["industry"]
    env = inc["env_type"].replace("_", " ")
    symptom = rng.choice(SYMPTOMS[inc["root_cause"]]).format(comp=comp_name, records=inc["records_affected"], env=env)
    hours = round((inc["resolved_at"] - inc["reported_at"]).total_seconds() / 3600, 1)
    extra = ""
    if inc["version"] == "v3.2" and inc["root_cause"] == "tile_cache_misconfig":
        extra = " The v3.2 upgrade reset the cache scale-level settings to defaults."
    elif inc["version"] == "v3.2" and inc["root_cause"] == "timeout":
        extra = " The problem first appeared shortly after the upgrade to v3.2."
    return f"""# Incident Case {inc['incident_id']}

- Incident ID: {inc['incident_id']}
- Project: {proj['name']} ({inc['project_id']}), {industry} sector
- Component: {comp_name} ({inc['component_id']})
- Environment: {env}, version {inc['version']}
- Severity: {inc['severity']}
- Reported: {inc['reported_at'].strftime(FMT)}
- Resolved: {inc['resolved_at'].strftime(FMT)} ({hours} hours)
- Root cause: {inc['root_cause']}
- Solution pattern: {inc['solution_pattern']}

## Symptoms
{symptom}

## Investigation
{INVESTIGATION[inc['root_cause']]}{extra}

## Root Cause
{ROOT_DESC[inc['root_cause']]}

## Resolution
{FIX_TEXT[inc['solution_pattern']]}

## Lessons Learned
{LESSON[inc['root_cause']]}

_Synthetic case generated from the incidents table for portfolio use._
"""


TROUBLESHOOTING = {
    "slow_queries_missing_index.md": ("Slow Queries After Large Data Loads", "missing_index",
        "Queries or map refreshes slow down sharply after a bulk load.",
        ["Inspect execution plans of the slowest queries for sequential scans.",
         "Verify composite and spatial indexes exist and are valid on the newly loaded tables.",
         "Check that table statistics are fresh (analyze / vacuum status).",
         "Compare query time before and after the load to confirm the load as the trigger."],
        ["Add a composite index on frequent filter columns.", "Rebuild the spatial index and refresh statistics."]),
    "connection_pool_exhausted.md": ("Connection Pool Exhaustion", "connection_pool_exhausted",
        "Intermittent 'cannot acquire connection' errors at peak load; recovery after restart.",
        ["Check pool metrics: active vs idle connections and wait time.",
         "Look for code paths that do not return connections (leaks).",
         "Compare pool size with peak concurrency across all nodes.",
         "Review idle and acquisition timeout settings."],
        ["Increase pool size and tune timeouts.", "Patch leaking data-access paths."]),
    "service_timeouts.md": ("Service and Gateway Timeouts", "timeout",
        "Requests fail with 504 or timeout errors, often at peak or in distributed environments.",
        ["Compare gateway, service and database timeouts for consistency.",
         "Check whether backends are still processing when the gateway gives up.",
         "In distributed deployments, check node-to-node latency and load balancer health checks.",
         "Review recent version upgrades for changed defaults."],
        ["Align timeouts and add bounded retry with backoff.", "Scale out the overloaded service."]),
    "tile_cache_issues.md": ("Tile Cache and Map Latency", "tile_cache_misconfig",
        "Map tiles are slow, blank or stale; cache hit rate drops.",
        ["Check cache hit rate and which scale levels miss.",
         "Compare cache scale-level settings with the published map service.",
         "Check whether a recent upgrade or republish reset cache settings.",
         "Verify cache storage capacity and permissions."],
        ["Restore cache scale-level configuration and re-seed tiles.", "Rebuild the cache for affected levels."]),
    "crs_mismatch.md": ("Coordinate System Mismatch", "crs_mismatch",
        "Layers are offset from the base map or spatial joins fail after loading data.",
        ["Compare the layer's spatial reference ID with the service's.",
         "Check source data metadata for a missing or wrong SRID.",
         "Test with a small sample before reloading the full dataset."],
        ["Reproject the layers to the service coordinate system.", "Correct the SRID metadata and reload."]),
    "service_publication_failures.md": ("Map Service Publication Failures", "service_publish_failure",
        "A map service fails to publish or starts in an error state.",
        ["Read the publishing log for the first error.",
         "Validate the service configuration and data source connections.",
         "Check the service account permissions on data and cache folders."],
        ["Republish with corrected configuration.", "Fix service account permissions."]),
    "bulk_import_performance.md": ("Bulk Import Performance and Failures", "batch_size_too_large",
        "Importing millions of records stalls, fails, or leaves the system slow afterwards.",
        ["Check the import batch size and transaction length first.",
         "Check whether indexes are rebuilt after every batch.",
         "Check memory and lock-wait metrics during the load.",
         "After the load, verify indexes and statistics (see slow query guide)."],
        ["Reduce batch size and pause index rebuilding until the load finishes."]),
    "authentication_failures.md": ("Authentication and SSO Failures", "authentication_failure",
        "Users are redirected to login, get 401 errors, or tokens fail validation.",
        ["Check auth service logs for signature or mapping errors.",
         "Verify token validity settings and signing keys.",
         "Compare SSO attribute mapping with the identity provider."],
        ["Correct token settings and refresh keys.", "Fix the SSO attribute mapping."]),
}
DEPLOYMENT = {
    "single_node_deployment.md": ("Single-Node Deployment Checklist",
        ["Confirm database, map service and cache share enough memory and disk.",
         "Set connection pool size within database limits.",
         "Run a smoke test of import, query and map rendering.",
         "Verify cache scale levels match the published service."]),
    "distributed_deployment.md": ("Distributed Deployment Pre-Go-Live Checklist",
        ["Align timeouts across load balancer, gateway, services and database.",
         "Size connection pools for total concurrency across all nodes.",
         "Verify clock synchronization and health-check intervals between nodes.",
         "Verify cache scale-level configuration on every node after install or upgrade.",
         "Run a load test at expected peak and review timeout and error rates.",
         "Test failover of one node while users are active."]),
    "cloud_deployment.md": ("Cloud Deployment Checklist",
        ["Confirm network paths and security groups between services.",
         "Configure autoscaling limits and database connection limits together.",
         "Verify SSO and token settings against the production identity provider.",
         "Run a smoke test and check cache configuration after provisioning."]),
}


def write_outputs(data, seed, fails, notes, cases):
    for sub in ("data/csv", "docs/historical_cases", "docs/troubleshooting_guides",
                "docs/deployment_guides", "ground_truth"):
        p = OUT / sub
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(parents=True, exist_ok=True)
    db = OUT / "data/delivery.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    build_db(conn, data)
    conn.close()
    for table, cols in COLUMNS.items():
        with open(OUT / f"data/csv/{table}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for r in data[table]:
                w.writerow(["" if cell(table, r, c) is None else cell(table, r, c) for c in cols])

    rng = Random(seed + 1)
    with open(OUT / "ground_truth/doc_manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["incident_id", "doc_path", "project_id", "component_id", "root_cause",
                    "solution_pattern", "version", "env_type", "records_affected_million"])
        for inc in data["docs"]:
            path = f"docs/historical_cases/{inc['incident_id']}.md"
            (OUT / path).write_text(render_case(inc, data, rng), encoding="utf-8")
            w.writerow([inc["incident_id"], path, inc["project_id"], inc["component_id"], inc["root_cause"],
                        inc["solution_pattern"], inc["version"], inc["env_type"], inc["records_affected"] or ""])
    with open(OUT / "ground_truth/incidents_truth.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["incident_id", "status", "true_root_cause", "true_solution_pattern", "has_doc", "cluster_tag",
                    "version", "env_type", "records_affected_million"])
        for i in data["incidents"]:
            w.writerow([i["incident_id"], i["status"], i["root_cause"], i["solution_pattern"], int(i["has_doc"]),
                        i["tag"] or "", i["version"], i["env_type"], i["records_affected"] or ""])
    for name, (title, root, symptom, steps, fixes) in TROUBLESHOOTING.items():
        body = (f"# {title}\n\nRelated root cause: `{root}`\n\n## Typical symptoms\n{symptom}\n\n"
                "## Recommended diagnostic order\n" + "\n".join(f"{n}. {s}" for n, s in enumerate(steps, 1)) +
                "\n\n## Common fixes\n" + "\n".join(f"- {s}" for s in fixes) + "\n")
        (OUT / "docs/troubleshooting_guides" / name).write_text(body, encoding="utf-8")
    for name, (title, items) in DEPLOYMENT.items():
        (OUT / "docs/deployment_guides" / name).write_text(
            f"# {title}\n\n" + "\n".join(f"- [ ] {s}" for s in items) + "\n", encoding="utf-8")
    (OUT / "ground_truth/reference_sql.sql").write_text(
        "\n".join(f"-- {k}{v}\n" for k, v in REFERENCE_SQL.items()), encoding="utf-8")
    (OUT / "ground_truth/evaluation_cases.json").write_text(
        json.dumps(dict(benchmark_version=BENCHMARK_VERSION, changelog=BENCHMARK_CHANGELOG,
                        reference_date=REF_DATE.strftime("%Y-%m-%d"), seed=seed, cases=cases),
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    c = Counter(i["status"] for i in data["incidents"])
    lines = [f"seed: {seed} (frozen)", f"reference date: {REF_DATE:%Y-%m-%d}",
             f"validation failures: {fails or 'none'}", ""]
    lines += [f"{k}: {v}" for k, v in notes.items()]
    lines += ["", f"incidents: {len(data['incidents'])}  status: {dict(c)}", f"case docs: {len(data['docs'])}",
              f"evaluation cases: {len(cases)}"]
    (OUT / "ground_truth/validation_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search-seed", action="store_true", help="development only: find the first passing seed")
    args = ap.parse_args()
    if args.search_seed:
        for seed in range(42, 5042):
            fails, _, _ = validate(generate(seed))
            if not fails:
                print(f"first passing seed: {seed}  -> set FROZEN_SEED = {seed}")
                return
        raise SystemExit("no passing seed found; review the rules or thresholds")
    data = generate(FROZEN_SEED)
    fails, notes, cases = validate(data)
    if fails:
        raise SystemExit("VALIDATION FAILED for frozen seed %d:\n- %s" % (FROZEN_SEED, "\n- ".join(fails)))
    write_outputs(data, FROZEN_SEED, fails, notes, cases)


if __name__ == "__main__":
    main()
